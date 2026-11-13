"""Stop-safe eval runs: every finished episode is on disk before the next one is counted, and a stopped run
resumes where it left off.

- Each finished episode is one line in results.jsonl, written with flush + fsync. A crash can cut off at most the
  line being written; the loader skips such a line.
- An episode interrupted by a stop (usage limit, unclean environment: guard.EvalStop) is never written, so it
  can't count as a failure. It runs again on resume.
- Episodes that ended in an ordinary error are written (so the run report shows them) and run again on resume.
- Rerunning the same command (same --label) skips every episode already done. run_config.json pins the settings:
  a resume with different settings is refused, so one results file never mixes two configurations.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

STOPPED_EXIT_CODE = 3
STOP_GRACE_S = 180.0  # after a stop, how long episodes already running may take to finish and be saved


def key(row: dict) -> tuple[str, int]:
    return str(row["task_id"]), int(row["trial"])


def load(path: Path) -> list[dict]:
    """All complete lines of a results file (a line cut off by a crash is skipped)."""
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def latest(rows: list[dict]) -> dict[tuple[str, int], dict]:
    """The last row per (task, trial): a retried episode replaces its earlier error."""
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        out[key(r)] = r
    return out


def write_json(path: Path, obj: Any) -> None:
    """Atomic: write a temp file, then rename over the target."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


def pin_config(out_dir: Path, config: dict) -> None:
    path = out_dir / "run_config.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old != config:
            diff = {k: (old.get(k), config.get(k)) for k in set(old) | set(config) if old.get(k) != config.get(k)}
            raise SystemExit(f"{out_dir.name} was started with other settings {diff}; use a new --label")
    else:
        write_json(path, config)


def append(f: Any, row: dict) -> None:
    f.write(json.dumps(row, default=str) + "\n")
    f.flush()
    os.fsync(f.fileno())


def run_jobs(jobs: list, job_key: Callable[[Any], tuple[str, int]], run_one: Callable[[Any], dict],
             out_dir: Path, concurrency: int, describe: Callable[[dict], str]) -> tuple[list[dict], str | None]:
    """Run the jobs not done yet. Returns (one row per job that has one, why the run stopped or None)."""
    path = out_dir / "results.jsonl"
    previous = latest(load(path))
    wanted = {job_key(j) for j in jobs}
    done = {k for k, r in previous.items() if "error" not in r and k in wanted}
    todo = [j for j in jobs if job_key(j) not in done]
    if done:
        print(f"resuming {out_dir.name}: {len(done)}/{len(jobs)} episodes already saved, {len(todo)} to run",
              flush=True)
    stopped, new, t0 = None, [], time.perf_counter()
    spent = sum(r.get("cost_usd", 0.0) for k, r in previous.items() if k in done)
    pool = ThreadPoolExecutor(max(concurrency, 1))
    pending = {pool.submit(run_one, j) for j in todo}
    deadline = None  # set at the first stop: running episodes get STOP_GRACE_S to finish, then we leave them
    with open(path, "a") as f:
        while pending:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            finished, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not finished:
                print(f"{len(pending)} episodes still running {STOP_GRACE_S:.0f} s after the stop; not waiting for "
                      "them (they rerun on resume)", flush=True)
                break
            for fut in finished:
                try:
                    r = fut.result()
                except BaseException as e:  # guard.EvalStop (or Ctrl-C): not saved, it reruns on resume
                    if stopped is None:
                        stopped = f"{type(e).__name__}: {e}"
                        print(f"STOPPING (nothing lost; unfinished episodes rerun on resume): {stopped}", flush=True)
                        pool.shutdown(wait=False, cancel_futures=True)
                        deadline = time.monotonic() + STOP_GRACE_S
                    continue
                append(f, r)
                new.append(r)
                spent += r.get("cost_usd", 0.0)
                n = len(done) + len(new)
                eta = (time.perf_counter() - t0) / len(new) * (len(jobs) - n)
                print(f"[{n}/{len(jobs)}] {describe(r)} · ${spent:.2f} API-equivalent so far · "
                      f"ETA {eta / 60:.0f} min", flush=True)
    pool.shutdown(wait=stopped is None)  # after a stop, never block on an episode stuck mid-call
    rows = latest([r for r in previous.values() if key(r) in wanted] + new)
    return list(rows.values()), stopped


def finish(stopped: str | None, rows: list[dict], planned: int, module: str, label: str) -> None:
    """Tell how to resume a stopped run (and exit non-zero), or which episodes errored."""
    saved = sum("error" not in r for r in rows)
    errors = planned - saved
    if stopped:
        args = sys.argv[1:] if "--label" in sys.argv else [*sys.argv[1:], "--label", label]
        print(f"\nSTOPPED SAFELY: {saved}/{planned} episodes saved. Resume after the limit resets with:\n"
              f"  uv run --group tau2 --group evals python -m {module} {' '.join(args)}", flush=True)
        if any(t is not threading.main_thread() and not t.daemon for t in threading.enumerate()):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(STOPPED_EXIT_CODE)  # an episode thread is stuck mid-call; a normal exit would wait for it
        sys.exit(STOPPED_EXIT_CODE)
    if errors:
        print(f"\n{errors} episodes errored or are missing; rerun the same command to retry only those.", flush=True)
