"""Voice channel (D26): a phone call through Twilio ConversationRelay, handled by the same agent and policy gate.

Twilio answers the call, turns the caller's speech into text and our text into speech. Between the two it holds a
websocket to us and sends JSON messages:
    setup      once, with the call's ids
    prompt     what the caller said (already transcribed): `voicePrompt`, `last`
    interrupt  the caller talked over us: `utteranceUntilInterrupt` = what they actually heard
    dtmf       a key press
We answer with `text` tokens (spoken as they arrive, `last: true` closes the reply) and `end` to hang up / hand off.

This module is transport-free: `handle()` takes one Twilio message plus the stored call state and a `send`
callback. api.py wires it to a websocket (local, any long-running host) and to API Gateway WebSocket → Lambda.

What changes for voice, and why (the rest is the chat agent unchanged):
  - replies are spoken: short sentences, no lists or markdown, ids read character by character
  - speech-to-text garbles emails and ids, so the agent reads them back before using them
  - consent: a hedged answer ("yeah, I guess") is not an explicit yes, so the gate doesn't count it
  - tools take seconds: if the reply isn't ready after FILLER_AFTER_S, say a short filler line
  - barge-in: the history keeps only what the caller heard, so the model doesn't think it said the rest
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any
from xml.sax.saxutils import quoteattr

from .agent import Agent, Turn, make_agent
from .gate import Session

GREETING = "Hi, this is the store's support line. I can help with cancellations, returns, exchanges and order " \
           "changes. What can I do for you?"
FILLER = "One moment while I check that."
GOODBYE = "We've reached the time limit for this demo call. Thanks for calling, goodbye."
HANDOFF = "I'll pass this to a human agent, who will follow up with you. Goodbye."
FILLER_AFTER_S = float(os.getenv("VOICE_FILLER_AFTER_S", "1.2"))
MAX_CALL_S = int(os.getenv("VOICE_MAX_CALL_S", "480"))
HINTS = "order number,return,exchange,cancel,gift card,PayPal,credit card,example.com"

VOICE_RULES = """

# This conversation is a phone call
Your replies are read aloud by text-to-speech and the customer's words come from speech-to-text.
- Speak in short, plain sentences: no markdown, lists, bullet points, emojis or symbols. At most three facts per
  reply; offer to give more.
- Speech-to-text often gets emails, names and ids wrong. Before you look up an email or name, read it back spelled
  out (for example "j, p, at example dot com") and wait for the customer to say it's right.
- Read order ids and item ids one character at a time, and amounts as dollars and cents.
- Before any change, read back the exact change and ask for a clear yes. "I guess", "maybe" or "I think so" is not
  a yes: ask again.
- If a message looks cut off or makes no sense, ask the customer to repeat it.
"""

_HEDGE = re.compile(r"\b(i guess|i think|i suppose|maybe|probably|not sure|kind of|sort of|i don'?t know)\b", re.I)


def spoken_consent(text: str) -> str:
    """The text the gate's confirmation check sees. A hedged answer is not an explicit yes on a phone call."""
    return "" if _HEDGE.search(text) else text


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def as_transcript(text: str) -> str:
    """Typed text → what speech-to-text tends to return: lowercase, no punctuation, emails said aloud
    ("j.doe@example.com" → "j dot doe at example dot com"), no "#" before ids. For the voice eval (no audio)."""
    text = _EMAIL.sub(lambda m: m.group(0).replace("@", " at ").replace(".", " dot "), text)
    text = text.replace("#", "")
    text = re.sub(r"[^\w\s'$]", " ", text)
    return " ".join(text.lower().split())


def voice_agent(gate: Any, client: Any) -> Agent:
    from .agent import system_prompt
    return make_agent(gate, client=client, system=system_prompt() + VOICE_RULES, name="voice")


def new_call(call_id: str, setup: dict, threshold: float) -> tuple[Session, list[dict], dict]:
    """State for a new call: the gate's session, the model's history, and the transcript/voice bookkeeping."""
    session = Session(session_id=call_id, approval_threshold=threshold)
    extra = {"transcript": [{"role": "assistant", "text": GREETING, "ts": time.time()}], "notified": [],
             "voice": {"call_sid": setup.get("callSid"), "started": time.time(), "turns": [], "pending": [],
                       "busy": False}}
    return session, [], extra


def speakable_chunks(text: str) -> list[str]:
    """Split a reply into sentence-sized tokens so text-to-speech can start on the first one."""
    text = re.sub(r"[*_#`>|]+", "", text)                      # markdown the model may still emit
    text = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", text, flags=re.M)  # list markers
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p + " " for p in parts if p]


def on_interrupt(messages: list[dict], extra: dict, heard: str) -> None:
    """Keep only what the caller heard of our last reply, in the model's history and in the transcript."""
    heard = heard.strip()
    note = f"{heard} [the caller interrupted here and did not hear the rest]" if heard else \
        "[the caller interrupted before hearing this reply]"
    for msg in reversed(messages):
        if msg["role"] == "assistant" and isinstance(msg["content"], list) and \
                any(b.get("type") == "text" for b in msg["content"]):
            msg["content"] = [b for b in msg["content"] if b.get("type") != "text"] + [{"type": "text", "text": note}]
            break
    for t in reversed(extra["transcript"]):
        if t["role"] == "assistant":
            t["heard"] = heard
            break


def take_turn(agent: Agent, session: Session, messages: list[dict], extra: dict, text: str,
              send: Callable[[dict], None], notices: list[str] | None = None) -> Turn:
    """One caller utterance → the agent's reply, spoken. Says a filler line if the reply is slow."""
    started = time.perf_counter()
    filler_s = None
    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(agent.respond, session, messages, text, notices=notices, gate_text=spoken_consent(text))
        try:
            turn = fut.result(timeout=FILLER_AFTER_S)
        except FutureTimeout:
            send({"type": "text", "token": FILLER, "last": True, "preemptible": True})
            filler_s = time.perf_counter() - started
            turn = fut.result()
    reply_s = time.perf_counter() - started
    # the written "Done: ..." record line repeats what the agent just said; on a call it isn't read out
    spoken = "\n".join(line for line in turn.reply.split("\n") if not line.startswith("Done: "))
    chunks = speakable_chunks(spoken) or ["Sorry, could you say that again? "]
    for i, chunk in enumerate(chunks):
        send({"type": "text", "token": chunk, "last": i == len(chunks) - 1})
    events = [{"tool": c["name"], "ok": c["result"].get("ok", False), "blocked": c["result"].get("blocked_by_policy"),
               "pending_approval": c["result"].get("pending_approval", False)} for c in turn.tool_calls]
    now = time.time()
    extra["transcript"] += [{"role": "user", "text": text, "ts": now, "hedged": spoken_consent(text) == "" and
                             bool(text.strip())},
                            {"role": "assistant", "text": turn.reply, "ts": now, "events": events}]
    # latency the caller feels: time to the first audio we sent (filler or answer), and to the real answer
    extra["voice"]["turns"].append({"first_audio_ms": round(1000 * (filler_s or reply_s)),
                                    "answer_ms": round(1000 * reply_s), "filler": filler_s is not None})
    if turn.transferred:
        send({"type": "text", "token": HANDOFF, "last": True})
        send({"type": "end", "handoffData": '{"reasonCode": "live-agent-handoff"}'})
    return turn


def over_time(extra: dict) -> bool:
    return time.time() - extra["voice"]["started"] > MAX_CALL_S


# ---------- TwiML + Twilio request signatures ----------
def twiml(ws_url: str, action_url: str | None) -> str:
    action = f" action={quoteattr(action_url)}" if action_url else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Connect{action}><ConversationRelay url={quoteattr(ws_url)} welcomeGreeting={quoteattr(GREETING)} '
            f'interruptible="any" hints={quoteattr(HINTS)} /></Connect></Response>')


def twiml_say(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{_xml(text)}</Say><Hangup/></Response>'


def _xml(text: str) -> str:
    return quoteattr(text)[1:-1]


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the full URL + every POST param (sorted, key then value)."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
