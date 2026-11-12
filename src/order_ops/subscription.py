"""Model calls on a Claude plan (Agent SDK + CLI login) instead of the API key, for evals (D27).

Every call runs under guard.py (isolated configuration, never an API key, only the known account-level hooks, stop at
80% of the 5-hour or weekly window). Three pieces:

  SubscriptionClient  stands in for `anthropic.Anthropic()` in calls without tools: the simulated customer, the NL
                      judge and the router (structured output). One Agent SDK query per call, no tools.
  SdkAgent            the agent (and each routed specialist) as one Agent SDK session per conversation. Its tools are
                      in-process MCP tools whose handlers run nothing: they hand the call to our code and wait for its
                      result. So `_create(history)` keeps the API path's step semantics (one model step = one tool
                      call or one reply), and Agent.respond, the router and the τ² adapter run unchanged: every tool
                      call still goes through the policy gate (or out to τ²) in this repo's code.
  ClaudeSubLLM        a litellm provider ("claude-sub/<model>") so τ²'s user simulator and judge use the same client.

Differences from the API path (the deploy-time parity check measures their effect): the model sees the tools as
mcp__store__<name>; parallel tool calls can't be switched off, so they are served one at a time; single calls with a
history (customer, judge) get it as one rendered transcript; no temperature setting; operator notices are a tagged
line in the user message; an account-level hook adds one sentence per session (guard.KNOWN_HOOKS).

Only importable with the eval dependency group:  uv sync --group evals
"""
from __future__ import annotations

import asyncio
import json
import queue
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)

from . import guard
from .agent import MAX_STEPS, Agent

SERVER = "store"
CALL_TIMEOUT_S = 900
GIVE_UP = "I'm sorry, I couldn't finish that. Let me transfer you to a human agent."
ZERO = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)

# ---------- one event loop for every SDK session in this process ----------
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_workdir = tempfile.mkdtemp(prefix="claude-eval-")  # empty working folder for the CLI


LIMIT_WORDS = ("usage limit", "rate limit", "rate_limit", "limit reached", "hit your limit", "limit will reset",
               "out of extra usage", "out_of_credits", "5-hour limit", "weekly limit", "session limit")


def failed(what: str, res: Any) -> BaseException:
    """The error for a failed CLI result. A plan limit becomes QuotaStop, so it stops the run (the episode is not
    saved and reruns on resume) instead of being scored as a failed episode."""
    text = f"{getattr(res, 'subtype', '')} {getattr(res, 'result', '') or ''} {getattr(res, 'errors', '') or ''}"
    if any(w in text.lower() for w in LIMIT_WORDS):
        return guard.QuotaStop(f"{what}: plan limit reported by the CLI: {text[:300]}")
    return RuntimeError(f"{what} failed: {text[:300]}")


def as_stop(e: BaseException) -> BaseException:
    """An SDK/CLI exception that reports a plan limit becomes QuotaStop; anything else is returned unchanged."""
    if isinstance(e, Exception) and any(w in str(e).lower() for w in LIMIT_WORDS):
        return guard.QuotaStop(f"plan limit reported by the CLI: {str(e)[:300]}")
    return e


def loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="claude-sdk", daemon=True).start()
    return _loop


def run(coro: Any, timeout: float = CALL_TIMEOUT_S) -> Any:
    """Run a coroutine on the SDK loop from any thread and wait for it."""
    return asyncio.run_coroutine_threadsafe(coro, loop()).result(timeout)


def options(system: str, model: str, effort: str | None, max_tokens: int, **kw: Any) -> ClaudeAgentOptions:
    env = guard.sdk_env() | {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)}
    return ClaudeAgentOptions(system_prompt=system, model=model, effort=effort, tools=[], setting_sources=[],
                              strict_mcp_config=True, cwd=_workdir, env=env, include_hook_events=True, **kw)


def usage_ns(u: dict | None) -> SimpleNamespace:
    u = u or {}
    return SimpleNamespace(**{k: u.get(k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                           "cache_creation_input_tokens")})


@dataclass
class Block:
    """A content block shaped like the anthropic SDK's (what Agent.respond and the τ² adapter read)."""
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def model_dump(self, exclude_none: bool = True) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        b = b if isinstance(b, dict) else b.model_dump()
        if b.get("type") == "text":
            parts.append(b["text"])
        elif b.get("type") in ("tool_use", "tool_result"):
            raise ValueError("tool blocks go through SdkAgent, not SubscriptionClient")
    return "".join(parts)


def render(messages: list[dict]) -> str:
    """A message list as one prompt. A single user message is sent as is; a history becomes a tagged transcript."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return _text(messages[0]["content"])
    turns = "\n".join(f"<{m['role']}>\n{_text(m['content'])}\n</{m['role']}>" for m in messages)
    return ("The conversation so far. You wrote the <assistant> turns; the <user> turns are the other side.\n"
            f"<conversation>\n{turns}\n</conversation>\n\nWrite your next message (the next <assistant> turn), "
            "only its text, without tags.")


# ---------- single calls without tools ----------
async def _one_shot(prompt: str, opts: ClaudeAgentOptions) -> ResultMessage:
    guard.before_run()
    result = None
    try:
        async for msg in query(prompt=prompt, options=opts):
            guard.check(msg)
            if isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:  # noqa: BLE001
        raise as_stop(e) from e
    if result is None:
        raise RuntimeError("the CLI ended without a result")
    return result


class _Messages:
    def create(self, *, model: str, messages: list[dict], system: Any = None, max_tokens: int = 4000,
               output_config: dict | None = None, tools: list | None = None, **_ignored: Any) -> SimpleNamespace:
        if tools:
            raise ValueError("calls with tools go through SdkAgent")
        cfg = output_config or {}
        fmt = cfg.get("format")
        sys_text = system if isinstance(system, str) else _text(system or [])
        opts = options(sys_text, model, cfg.get("effort", "low"), max_tokens, max_turns=3 if fmt else 1,
                       output_format=fmt)
        started = time.perf_counter()
        res = run(_one_shot(render(messages), opts))
        if res.is_error:
            raise failed("CLI call", res)
        text = json.dumps(res.structured_output) if fmt else (res.result or "")
        return SimpleNamespace(content=[Block("text", text=text)], usage=usage_ns(res.usage), model=model,
                               stop_reason="refusal" if res.stop_reason == "refusal" else "end_turn",
                               latency_ms=round((time.perf_counter() - started) * 1000))


class SubscriptionClient:
    """Duck-types `anthropic.Anthropic()` for calls without tools; `backend` tells make_agent to build SdkAgents."""
    backend = "subscription"

    def __init__(self) -> None:
        guard.sdk_env()  # fail fast (NotClean) if the isolated login isn't set up
        self.messages = _Messages()


# ---------- the agent's tool loop ----------
class SdkAgent(Agent):
    """Agent whose model steps run in one Agent SDK session per conversation (see the module docstring)."""

    def __init__(self, gate: Any, **kw: Any):
        kw.pop("client", None)
        super().__init__(gate, client=SimpleNamespace(backend="subscription"), **kw)
        self._sdk: ClaudeSDKClient | None = None
        self._events: queue.Queue = queue.Queue()
        self._pending: dict[str, asyncio.Future] = {}  # tool_use id → handler waiting for our result
        self._seen = 0  # how much of the caller's history was already passed to the session
        self._busy = False  # a query is running (its ResultMessage not read yet)
        self.parallel_tool_messages = 0  # model messages with >1 tool call (the API path disables them)

    # --- SDK side (runs on the SDK loop) ---
    def _handler(self, name: str) -> Any:
        async def handle(args: dict) -> dict:
            fut = asyncio.get_running_loop().create_future()
            tid = "toolu_sdk_" + uuid.uuid4().hex[:16]
            self._pending[tid] = fut
            self._events.put(("tool", tid, name, dict(args)))
            content, is_error = await fut
            return {"content": [{"type": "text", "text": content}], "is_error": is_error}
        return handle

    async def _start(self, text: str) -> None:
        if self._sdk is None:
            tools = [SdkMcpTool(name=t["name"], description=t["description"], input_schema=t["input_schema"],
                                handler=self._handler(t["name"])) for t in self.tools]
            opts = options(self.system, self.model, self.effort, 8000,
                           mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
                           allowed_tools=[f"mcp__{SERVER}__*"], max_turns=MAX_STEPS + 5)
            self._sdk = ClaudeSDKClient(opts)
            await self._sdk.connect()
        await self._sdk.query(text)
        asyncio.get_running_loop().create_task(self._consume())

    async def _consume(self) -> None:
        try:
            async for msg in self._sdk.receive_response():
                guard.check(msg)
                if isinstance(msg, AssistantMessage) and sum(isinstance(b, ToolUseBlock) for b in msg.content) > 1:
                    self.parallel_tool_messages += 1
                if isinstance(msg, ResultMessage):
                    self._events.put(("done", msg))
                    return
            self._events.put(("error", RuntimeError("the session ended without a result")))
        except BaseException as e:  # noqa: BLE001 - handed to the caller's thread, which raises it
            self._events.put(("error", as_stop(e)))

    # --- caller side (episode thread) ---
    def _resolve(self, tid: str, content: str, is_error: bool) -> None:
        fut = self._pending.pop(tid, None)
        if fut is not None:
            loop().call_soon_threadsafe(lambda: fut.done() or fut.set_result((content, is_error)))

    def _get(self) -> tuple:
        try:
            ev = self._events.get(timeout=CALL_TIMEOUT_S)
        except queue.Empty:
            raise RuntimeError(f"no model step within {CALL_TIMEOUT_S}s") from None
        if ev[0] == "error":
            self._busy = False
            raise ev[1]
        return ev

    def _account(self, res: ResultMessage) -> None:
        self._busy = False
        self.usage.add(self.model, usage_ns(res.usage), res.duration_api_ms or 0)
        self.usage.calls += max(res.num_turns or 1, 1) - 1

    def _end_turn(self) -> None:
        """Finish the running query (after a handoff or a step limit): answer leftover calls, read its result."""
        stop = json.dumps({"ok": False, "error": "This turn has ended; the call was not run."})
        for tid in list(self._pending):
            self._resolve(tid, stop, True)
        while self._busy:
            ev = self._get()
            if ev[0] == "tool":
                self._resolve(ev[1], stop, True)
            else:
                self._account(ev[1])

    def _create(self, messages: list[dict]) -> SimpleNamespace:
        """One model step, like the API path: pass on what the caller added to the history, return the next step."""
        new, self._seen = messages[self._seen:], len(messages)
        texts: list[str] = []
        for m in new:
            if m["role"] == "assistant":
                continue  # our own steps, echoed back into the caller's history
            if m["role"] == "system":
                texts.append(f"[Operator notice: {m['content']}]")
            elif isinstance(m["content"], str):
                texts.append(m["content"])
            else:
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        self._resolve(b["tool_use_id"], _text_of(b.get("content")), bool(b.get("is_error")))
                    elif b.get("type") == "text":
                        texts.append(b["text"])
        if texts:
            self._end_turn()
            guard.before_run()
            try:
                run(self._start("\n\n".join(texts)))
            except Exception as e:  # noqa: BLE001
                raise as_stop(e) from e
            self._busy = True
        elif not self._busy:
            raise RuntimeError("nothing new to send to the model")
        ev = self._get()
        if ev[0] == "tool":
            _, tid, name, args = ev
            return SimpleNamespace(stop_reason="tool_use", usage=ZERO,
                                   content=[Block("tool_use", id=tid, name=name, input=args)])
        res: ResultMessage = ev[1]
        self._account(res)
        if res.is_error and res.subtype != "error_max_turns":
            raise failed("agent step", res)
        text = GIVE_UP if res.subtype == "error_max_turns" else (res.result or "")
        return SimpleNamespace(stop_reason="refusal" if res.stop_reason == "refusal" else "end_turn", usage=ZERO,
                               content=[Block("text", text=text)])

    def close(self) -> None:
        if self._sdk is None:
            return
        for tid in list(self._pending):
            self._resolve(tid, json.dumps({"ok": False, "error": "closed"}), True)
        try:
            run(self._sdk.disconnect(), timeout=30)
        except Exception:  # noqa: BLE001 - the CLI process is gone either way
            pass
        self._sdk, self._busy = None, False


def _text_of(content: Any) -> str:
    if content is None or isinstance(content, str):
        return content or ""
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


# ---------- litellm provider for τ² (customer + judge) ----------
PROVIDER = "claude-sub"


def register_litellm() -> None:
    import litellm
    from litellm import CustomLLM, ModelResponse

    client = SubscriptionClient()

    class ClaudeSubLLM(CustomLLM):
        def completion(self, model: str, messages: list, api_base: str = "", custom_prompt_dict: dict | None = None,
                       model_response: Any = None, print_verbose: Any = None, encoding: Any = None,
                       api_key: Any = None, logging_obj: Any = None, optional_params: dict | None = None,
                       *args: Any, **kwargs: Any) -> ModelResponse:
            opt = optional_params or {}
            if opt.get("tools"):
                raise ValueError(f"{PROVIDER} serves calls without tools only")
            system = "\n\n".join(_text(m["content"]) for m in messages if m["role"] == "system")
            conv = [{"role": m["role"], "content": _text(m.get("content") or "")}
                    for m in messages if m["role"] != "system"]
            resp = client.messages.create(model=model.split("/")[-1], system=system, messages=conv,
                                          max_tokens=opt.get("max_tokens") or 4000,
                                          output_config=opt.get("output_config") or {"effort": "low"})
            u = resp.usage
            prompt = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
            return ModelResponse(model=f"{PROVIDER}/{model.split('/')[-1]}",
                                 choices=[{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": resp.content[0].text}}],
                                 usage={"prompt_tokens": prompt, "completion_tokens": u.output_tokens,
                                        "total_tokens": prompt + u.output_tokens})

    litellm.custom_provider_map = [p for p in litellm.custom_provider_map if p["provider"] != PROVIDER] + [
        {"provider": PROVIDER, "custom_handler": ClaudeSubLLM()}]


def usage_now() -> dict:
    """Latest 5-hour / weekly utilization seen in this process (from the CLI's rate-limit events)."""
    return {k: v.get("utilization") for k, v in (guard.last_windows or {}).items()}
