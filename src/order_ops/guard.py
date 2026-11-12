"""Guard for every Claude Agent SDK run: CLI login in an isolated configuration, never an API key, usage guard.

Evals run the model on a Claude plan through the Agent SDK (CLI token from `claude setup-token`); the deployed app
uses the API key. Every run:

  1. isolated configuration: CLAUDE_CONFIG_DIR is an empty folder (no user plugins, hooks, settings or memory files),
     logged in with the token in ~/.config/claude-eval/oauth_token, run from an empty working folder;
  2. never an API key: API-key variables are removed from this process (the SDK merges its env over ours), and a run
     stops unless the CLI reports apiKeySource == "none";
  3. hooks: only KNOWN_HOOKS (account-level hooks, which no client setting can turn
     off) with their exact output; anything else stops the run;
  4. usage guard: after each rate-limit report, at or above CLAUDE_EVAL_STOP_AT (default 80%) of the 5-hour or the
     weekly window, the next run raises QuotaStop; paid extra usage (isUsingOverage) stops at once.

EvalStop derives from BaseException so task-level `except Exception` handlers can't swallow "stop the run".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_EVAL_HOME", Path.home() / ".config" / "claude-eval"))
TOKEN, CONFIG = HOME / "oauth_token", HOME / "config"
STOP_AT = float(os.environ.get("CLAUDE_EVAL_STOP_AT", "0.80"))
# Optional tighter cap for the weekly window only (e.g. 0.55 to leave room for other projects this week).
STOP_AT_WEEKLY = float(os.environ.get("CLAUDE_EVAL_STOP_AT_WEEKLY", str(STOP_AT)))
REMOVE = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
          "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")


def _local(name: str) -> dict:
    """Account-specific settings kept next to the token, outside the repo ({} when absent)."""
    p = HOME / name
    return json.loads(p.read_text()) if p.exists() else {}


KNOWN_HOOKS = {
    "UserPromptSubmit": "",
    "PostToolUse": "",  # after each tool call; empty output = the tool result reaches the model unchanged
    **_local("known_hooks.json"),  # account-level hooks and their exact output
}

last_windows: dict | None = None


class EvalStop(BaseException):
    """Stop the whole run (not a per-task failure)."""


class QuotaStop(EvalStop):
    """Usage reached the stop threshold, or paid extra usage was used."""


class NotClean(EvalStop):
    """The run would not use the CLI login in an isolated configuration."""


def read_token() -> str:
    """Copying the token from a terminal can wrap it across lines, so all whitespace is removed."""
    return "".join(TOKEN.read_text().split())


def sdk_env() -> dict[str, str]:
    """Environment for the SDK's CLI subprocess. Also scrubs API-key variables from this process."""
    for k in REMOVE:
        os.environ.pop(k, None)
    if not (TOKEN.exists() and CONFIG.is_dir()):
        raise NotClean(f"isolated CLI setup missing ({TOKEN}, {CONFIG}); run `claude setup-token` and save it there")
    tok = read_token()
    if tok.startswith("sk-ant-api"):
        raise NotClean(f"{TOKEN} holds an API key, not a CLI login token")
    # Account-level settings that let tool results reach the model unchanged (extra_env.json, outside the
    # repo); the guard still checks that every PostToolUse hook returns empty output.
    return {"CLAUDE_CONFIG_DIR": str(CONFIG), "CLAUDE_CODE_OAUTH_TOKEN": tok,
            **_local("extra_env.json")}


def utilization(w: dict | None = None) -> float:
    return max((x.get("utilization") or 0.0 for x in ((w if w is not None else last_windows) or {}).values()),
               default=0.0)


def limit_for(window: str) -> float:
    return STOP_AT_WEEKLY if window.startswith("seven_day") else STOP_AT


def before_run() -> None:
    for name, w in (last_windows or {}).items():
        used = w.get("utilization") or 0.0
        if used >= limit_for(name):
            raise QuotaStop(f"{name} usage at {used:.0%} (stop at {limit_for(name):.0%}): {last_windows}")


def check(msg) -> None:  # noqa: ANN001 - any Agent SDK message
    """Inspect one SDK message; raise EvalStop when a guarantee is broken."""
    global last_windows
    kind = type(msg).__name__
    if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
        src = (msg.data or {}).get("apiKeySource")
        if src != "none":
            raise NotClean(f"not using the CLI login: apiKeySource={src!r}")
    elif kind == "HookEventMessage" or (kind == "SystemMessage"
                                        and str(getattr(msg, "subtype", "")).startswith("hook")):
        data = getattr(msg, "data", {}) or {}
        event = (getattr(msg, "hook_event_name", None) or data.get("hook_event")
                 or str(data.get("hook_name", "")).split(":")[0])
        sub = getattr(msg, "subtype", "") or data.get("subtype", "")
        if event not in KNOWN_HOOKS:
            raise NotClean(f"unexpected hook {event!r}")
        if "response" in str(sub) and str(data.get("output") or "").strip() != KNOWN_HOOKS[event]:
            raise NotClean(f"hook {event!r} injected unexpected text: {str(data.get('output'))[:120]!r}")
    elif kind == "RateLimitEvent":
        info = msg.rate_limit_info
        raw = getattr(info, "raw", None) or {}
        last_windows = raw.get("unifiedWindows") or last_windows
        if raw.get("isUsingOverage"):
            raise QuotaStop(f"the run used paid extra usage: {raw}")
        if str(getattr(info, "status", "")) in ("rejected", "RateLimitStatus.REJECTED"):
            raise QuotaStop(f"usage limit reached: {raw}")
