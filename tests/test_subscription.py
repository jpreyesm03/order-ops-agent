"""The subscription backend without the CLI: the SDK session is replaced by a scripted fake model."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("claude_agent_sdk")

from order_ops import guard, subscription  # noqa: E402
from order_ops.agent import make_agent  # noqa: E402
from order_ops.subscription import SdkAgent, render  # noqa: E402


def result(text: str, turns: int = 2) -> SimpleNamespace:
    return SimpleNamespace(subtype="success", is_error=False, stop_reason="end_turn", result=text, num_turns=turns,
                           duration_api_ms=7, usage={"input_tokens": 10, "output_tokens": 5,
                                                     "cache_read_input_tokens": 100})


class FakeSession:
    def __init__(self):
        self.customer: list[str] = []

    def on_customer_message(self, text: str) -> None:
        self.customer.append(text)


class FakeGate:
    def __init__(self):
        self.calls: list[tuple] = []

    def run(self, session, name, args):  # noqa: ANN001
        self.calls.append((name, args))
        if name == "cancel_pending_order":
            return {"ok": False, "blocked_by_policy": "needs_confirmation", "error": "ask first"}
        return {"ok": True, "result": {"order_id": args.get("order_id"), "status": "pending"}}


def scripted(model_steps):  # noqa: ANN001
    """A fake `_start`: for each query, call the given tools through the real handlers, then finish."""
    async def fake_start(self, text):  # noqa: ANN001
        self.queries = getattr(self, "queries", []) + [text]
        self.tool_outputs = getattr(self, "tool_outputs", [])

        async def script():
            for name, args in model_steps(text):
                self.tool_outputs.append(await self._handler(name)(args))
            self._events.put(("done", result(f"reply to: {text[-20:]}")))
        asyncio.get_running_loop().create_task(script())
    return fake_start


def agent(monkeypatch, steps, **kw) -> SdkAgent:  # noqa: ANN001
    monkeypatch.setattr(SdkAgent, "_start", scripted(steps))
    return make_agent(FakeGate(), client=SimpleNamespace(backend="subscription"), system="sys", **kw)


def test_make_agent_picks_backend():
    assert type(make_agent(FakeGate(), client=SimpleNamespace(backend="subscription"), system="s")) is SdkAgent
    assert type(make_agent(FakeGate(), client=SimpleNamespace(), system="s")).__name__ == "Agent"


def test_tool_calls_go_through_the_gate_and_back(monkeypatch):
    a = agent(monkeypatch, lambda text: [("get_order_details", {"order_id": "#W1"}),
                                         ("cancel_pending_order", {"order_id": "#W1", "reason": "no longer needed"})])
    session, history = FakeSession(), []
    turn = a.respond(session, history, "cancel #W1 please")
    assert [c["name"] for c in turn.tool_calls] == ["get_order_details", "cancel_pending_order"]
    assert [n for n, _ in a.gate.calls] == ["get_order_details", "cancel_pending_order"]
    # the model got the gate's results, unchanged, with the error flag
    first, second = a.tool_outputs
    assert json.loads(first["content"][0]["text"])["result"]["status"] == "pending" and not first["is_error"]
    assert json.loads(second["content"][0]["text"])["blocked_by_policy"] == "needs_confirmation"
    assert second["is_error"]
    assert turn.reply.startswith("reply to:")
    assert a.usage.input == 10 and a.usage.cache_read == 100 and a.usage.calls == 2
    a.respond(session, history, "yes")  # second turn reuses the session; only the new message is sent
    assert a.queries == ["cancel #W1 please", "yes"]
    assert session.customer == ["cancel #W1 please", "yes"]


def test_notices_are_tagged_into_the_user_message(monkeypatch):
    a = agent(monkeypatch, lambda text: [])
    a.respond(FakeSession(), [], "hi", notices=["approval apr_1 was approved"])
    assert a.queries == ["hi\n\n[Operator notice: approval apr_1 was approved]"]


def test_handoff_ends_the_turn_and_is_drained_before_the_next(monkeypatch):
    a = agent(monkeypatch, lambda text: [("handoff", {"to": "returns", "reason": "delivered order"})]
              if "return" in text else [])
    session, history = FakeSession(), []
    turn = a.respond(session, history, "I want to return something")
    assert turn.reply == "" and turn.tool_calls[0]["name"] == "handoff"
    assert a._busy  # the specialist's session still has to finish its turn
    turn = a.respond(session, history, "thanks")
    assert turn.reply == "reply to: thanks" and not a._busy
    assert a.usage.calls == 4  # both queries accounted


def test_quota_stop_before_a_new_query(monkeypatch):
    a = agent(monkeypatch, lambda text: [])
    monkeypatch.setattr(guard, "last_windows", {"five_hour": {"utilization": 0.81}, "seven_day": {"utilization": 0.2}})
    with pytest.raises(guard.QuotaStop):
        a.respond(FakeSession(), [], "hi")


def test_render():
    assert render([{"role": "user", "content": "hello"}]) == "hello"
    out = render([{"role": "user", "content": "Hi, how can I help?"},
                  {"role": "assistant", "content": [{"type": "text", "text": "I need a refund"}]},
                  {"role": "user", "content": "Sure, your email?"}])
    assert "<assistant>\nI need a refund\n</assistant>" in out and out.endswith("without tags.")


# ---------- guard ----------
def _msg(kind: str, **attrs) -> object:
    return type(kind, (), attrs)()


def test_guard_rejects_api_key_and_unknown_hooks():
    with pytest.raises(guard.NotClean):
        guard.check(_msg("SystemMessage", subtype="init", data={"apiKeySource": "ANTHROPIC_API_KEY"}))
    guard.check(_msg("SystemMessage", subtype="init", data={"apiKeySource": "none"}))
    with pytest.raises(guard.NotClean):
        guard.check(_msg("SystemMessage", subtype="hook_response", data={"hook_name": "Stop", "output": ""}))
    with pytest.raises(guard.NotClean):  # a known hook that injects text into a tool result
        guard.check(_msg("SystemMessage", subtype="hook_response",
                         data={"hook_name": "PostToolUse:mcp__store__x", "output": "replaced"}))
    guard.check(_msg("SystemMessage", subtype="hook_response", data={"hook_name": "PostToolUse:x", "output": ""}))


def test_guard_stops_on_overage(monkeypatch):
    monkeypatch.setattr(guard, "last_windows", None)
    info = SimpleNamespace(status="allowed", raw={"isUsingOverage": True, "unifiedWindows": {}})
    with pytest.raises(guard.QuotaStop):
        guard.check(_msg("RateLimitEvent", rate_limit_info=info))
    assert issubclass(guard.EvalStop, BaseException) and not issubclass(guard.EvalStop, Exception)


def test_env_never_carries_an_api_key(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "oauth_token").write_text("sk-ant-oat01-abc\n def\n")
    monkeypatch.setattr(guard, "TOKEN", tmp_path / "oauth_token")
    monkeypatch.setattr(guard, "CONFIG", tmp_path / "config")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    env = guard.sdk_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abcdef" and "ANTHROPIC_API_KEY" not in env
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ
    (tmp_path / "oauth_token").write_text("sk-ant-api03-x")
    with pytest.raises(guard.NotClean):
        guard.sdk_env()
    assert subscription.PROVIDER == "claude-sub"
