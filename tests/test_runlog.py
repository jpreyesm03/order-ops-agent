"""Stop-safe runs: a usage stop loses no finished episode, never saves a half-run one, and resume finishes the rest."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from order_ops import guard, runlog

JOBS = [(f"t{n}", trial) for n in range(5) for trial in range(2)]  # 10 episodes


def key(job):  # noqa: ANN001
    return job


def episode(stop_after: int | None = None, error_on: set | None = None):  # noqa: ANN201
    ran = []

    def run_one(job):  # noqa: ANN001
        if stop_after is not None and len(ran) >= stop_after:
            raise guard.QuotaStop("usage at 80%")  # what the guard raises mid-episode
        ran.append(job)
        if error_on and job in error_on:
            return {"task_id": job[0], "trial": job[1], "reward": 0.0, "error": "RuntimeError: network"}
        return {"task_id": job[0], "trial": job[1], "reward": 1.0, "cost_usd": 0.01}
    return run_one, ran


def test_stop_keeps_finished_episodes_and_resume_runs_only_the_rest(tmp_path):
    run_one, ran = episode(stop_after=4)
    rows, stopped = runlog.run_jobs(JOBS, key, run_one, tmp_path, 1, str)
    assert stopped and "QuotaStop" in stopped
    saved = runlog.load(tmp_path / "results.jsonl")
    assert len(saved) == 4 and all("error" not in r for r in saved)  # interrupted episodes are not saved

    run_one, ran = episode()
    rows, stopped = runlog.run_jobs(JOBS, key, run_one, tmp_path, 1, str)
    assert stopped is None and len(ran) == 6  # only the 6 missing ones ran
    assert len(rows) == 10 and {runlog.key(r) for r in rows} == {(t, i) for t, i in JOBS}

    run_one, ran = episode()  # third time: nothing to do
    runlog.run_jobs(JOBS, key, run_one, tmp_path, 1, str)
    assert ran == []


def test_errored_episodes_are_retried_and_replaced(tmp_path):
    run_one, _ = episode(error_on={("t1", 0)})
    rows, _ = runlog.run_jobs(JOBS, key, run_one, tmp_path, 2, str)
    assert sum("error" in r for r in rows) == 1
    run_one, ran = episode()
    rows, _ = runlog.run_jobs(JOBS, key, run_one, tmp_path, 2, str)
    assert ran == [("t1", 0)] and len(rows) == 10 and not any("error" in r for r in rows)


def test_line_cut_off_by_a_crash_is_skipped(tmp_path):
    good = json.dumps({"task_id": "t0", "trial": 0, "reward": 1.0})
    (tmp_path / "results.jsonl").write_text(good + "\n" + '{"task_id": "t0", "tri')
    assert len(runlog.load(tmp_path / "results.jsonl")) == 1
    run_one, ran = episode()
    rows, _ = runlog.run_jobs(JOBS, key, run_one, tmp_path, 1, str)
    assert len(ran) == 9 and len(rows) == 10


def test_resume_with_other_settings_is_refused(tmp_path):
    runlog.pin_config(tmp_path, {"arch": "single"})
    runlog.pin_config(tmp_path, {"arch": "single"})
    with pytest.raises(SystemExit):
        runlog.pin_config(tmp_path, {"arch": "routed"})


def test_stopped_run_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as e:
        runlog.finish("QuotaStop: x", [{"task_id": "a", "trial": 0}], 3, "order_ops.evaluate", "lbl")
    assert e.value.code == runlog.STOPPED_EXIT_CODE and "--label lbl" in capsys.readouterr().out


def test_limit_messages_from_the_cli_stop_the_run():
    sub = pytest.importorskip("order_ops.subscription")
    res = SimpleNamespace(subtype="success", result="You've hit your limit · resets 3am", errors=None)
    assert isinstance(sub.failed("agent step", res), guard.QuotaStop)
    assert isinstance(sub.failed("agent step", SimpleNamespace(subtype="error_during_execution",
                                                               result="API Error: 500", errors=None)), RuntimeError)
    assert isinstance(sub.as_stop(RuntimeError("Claude AI usage limit reached|1731400000")), guard.QuotaStop)
    assert isinstance(sub.as_stop(RuntimeError("connection reset")), RuntimeError)


def test_stop_does_not_wait_for_an_episode_stuck_mid_call(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr(runlog, "STOP_GRACE_S", 0.3)
    release = threading.Event()

    def run_one(job):  # noqa: ANN001
        if job == ("t0", 0):
            release.wait(10)  # stuck, like a CLI call that never returns after the quota stop
            return {"task_id": job[0], "trial": job[1], "reward": 1.0}
        if job == ("t0", 1):
            time.sleep(0.05)
            raise guard.QuotaStop("usage at 80%")
        return {"task_id": job[0], "trial": job[1], "reward": 1.0}

    started = time.monotonic()
    rows, stopped = runlog.run_jobs(JOBS[:2], key, run_one, tmp_path, 2, str)
    release.set()
    assert stopped and time.monotonic() - started < 3
    assert runlog.load(tmp_path / "results.jsonl") == []  # neither the stopped nor the stuck episode is saved
