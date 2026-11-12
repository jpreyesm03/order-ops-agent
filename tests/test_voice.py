"""Voice channel (D26) with a fake Twilio ConversationRelay over a real websocket and a scripted model."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from order_ops import api, db, voice
from test_agent_loop import FakeClient, say, tool

SECRET = "relay-secret"


@pytest.fixture
def client(monkeypatch):
    conn = db.connect()
    schema = f"v_{uuid.uuid4().hex[:8]}"  # each test gets its own copy of the store; calls really cancel orders
    db.clone_schema(conn, "base", schema)
    monkeypatch.setattr(api, "LIVE", schema)
    monkeypatch.setenv("VOICE_RELAY_SECRET", SECRET)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-token")
    monkeypatch.setenv("VOICE_WS_URL", "wss://voice.example.test/ws")
    monkeypatch.setenv("VOICE_PUBLIC_URL", "https://api.example.test")
    with TestClient(api.app) as c:
        yield c
    db.drop_schema(conn, schema)
    conn.close()


class SlowClient(FakeClient):
    def create(self, **kw):
        time.sleep(0.05)
        return super().create(**kw)


def _small_pending_order() -> tuple[str, str]:
    conn = db.connect()
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {api.LIVE}.orders o
                                JOIN {api.LIVE}.order_payments p USING (order_id)
                                WHERE o.status = 'pending' AND p.amount < 200 ORDER BY random() LIMIT 1""").fetchone()
    email = db.get_user(conn, api.LIVE, uid)["email"]
    conn.close()
    return oid, email


def _reply(ws) -> list[dict]:
    """Everything Twilio would receive for one caller utterance: up to the answer's last token (or an end)."""
    out = []
    while True:
        m = ws.receive_json()
        out.append(m)
        if m["type"] == "end" or (m["type"] == "text" and m["last"] and m["token"] != voice.FILLER
                                  and m["token"] != voice.HANDOFF):
            return out


def test_unit_helpers():
    assert voice.spoken_consent("yeah, I guess") == "" and voice.spoken_consent("yes please") == "yes please"
    assert voice.speakable_chunks("**Done.** Your order:\n- #W1 is cancelled! Anything else?") == [
        "Done. ", "Your order: W1 is cancelled! ", "Anything else? "]  # no "#": TTS would say "hash"
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "Your refund of $90 goes to PayPal."}]}]
    extra = {"transcript": [{"role": "assistant", "text": "Your refund of $90 goes to PayPal."}]}
    voice.on_interrupt(msgs, extra, "Your refund of")
    assert msgs[0]["content"][0]["text"].startswith("Your refund of [the caller interrupted")
    assert extra["transcript"][0]["heard"] == "Your refund of"
    assert voice.as_transcript("Hi! My email is Daiki.Sanchez@example.com, order #W9348897.") == \
        "hi my email is daiki dot sanchez at example dot com order w9348897"
    sig = voice.twilio_signature("tok", "https://x.test/voice/incoming", {"From": "+1", "CallSid": "CA1"})
    assert voice.valid_signature("tok", "https://x.test/voice/incoming", {"CallSid": "CA1", "From": "+1"}, sig)
    assert not voice.valid_signature("tok", "https://x.test/voice/incoming", {"CallSid": "CA2", "From": "+1"}, sig)


def test_incoming_call_twiml(client, monkeypatch):
    conn = db.connect()
    conn.execute("DELETE FROM public.rate_limits WHERE bucket LIKE 'voice:%'")
    conn.close()
    monkeypatch.setattr(api, "VOICE_CALLS_PER_DAY", 1)
    form = {"CallSid": "CA123", "From": "+15550100"}
    sig = voice.twilio_signature("twilio-token", "https://api.example.test/voice/incoming", form)
    r = client.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<ConversationRelay" in r.text
    assert f'url="wss://voice.example.test/ws?t={SECRET}"' in r.text and 'interruptible="any"' in r.text
    assert client.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    capped = client.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": sig})
    assert "today's call limit" in capped.text and "<Hangup/>" in capped.text  # cost cap on a public number
    monkeypatch.delenv("TWILIO_AUTH_TOKEN")
    assert "not set up" in client.post("/voice/incoming", data=form).text


def test_websocket_needs_secret(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/ws?t=wrong") as ws:
        ws.receive_json()


def test_call_hedged_yes_is_not_consent(client, monkeypatch):
    """A whole call: authenticate, a hedged "yeah, I guess" is blocked by the gate, a clear yes cancels."""
    oid, email = _small_pending_order()
    monkeypatch.setattr(voice, "FILLER_AFTER_S", 0.01)  # every model call takes 50 ms: the filler line goes first
    api._client = SlowClient([
        tool("find_user_id_by_email", email=email), say("Thanks, I found your account. What can I do?"),
        tool("cancel_pending_order", order_id=oid, reason="no longer needed"),
        say("I can cancel it. Is that right?"),
        tool("cancel_pending_order", order_id=oid, reason="no longer needed"),
        say("I need a clear yes to cancel it. Should I cancel it?"),
        tool("cancel_pending_order", order_id=oid, reason="no longer needed"),
        say("Done. Your order is cancelled."),
        say("Is there anything else I can help with?"),
    ])
    with client.websocket_connect(f"/voice/ws?t={SECRET}") as ws:
        ws.send_json({"type": "setup", "callSid": "CA42", "from": "+15550100"})
        ws.send_json({"type": "prompt", "voicePrompt": f"Hi, my email is {email}", "last": True})
        first = _reply(ws)
        assert first[0] == {"type": "text", "token": voice.FILLER, "last": True, "preemptible": True}
        assert "".join(m["token"] for m in first[1:]) == "Thanks, I found your account. What can I do? "
        ws.send_json({"type": "prompt", "voicePrompt": "I want to cancel my order, no longer needed", "last": True})
        _reply(ws)
        ws.send_json({"type": "prompt", "voicePrompt": "yeah, I guess", "last": True})
        assert "Should I cancel it?" in "".join(m["token"] for m in _reply(ws))
        conn = db.connect()
        assert db.get_order(conn, api.LIVE, oid)["status"] == "pending"
        ws.send_json({"type": "prompt", "voicePrompt": "Yes, cancel it.", "last": True})
        assert "".join(m["token"] for m in _reply(ws)[1:]).startswith("Done. Your order is cancelled. Done: Order ")
        assert db.get_order(conn, api.LIVE, oid)["status"] == "cancelled"
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "Done.", "durationUntilInterruptMs": 300})
        ws.send_json({"type": "prompt", "voicePrompt": "", "last": True})  # empty transcript: ignored
        ws.send_json({"type": "dtmf", "digit": "1"})  # its reply proves the two messages above were handled
        assert _reply(ws)[-1]["token"] == "Is there anything else I can help with? "
    sid = conn.execute(f"SELECT session_id FROM {api.LIVE}.sessions WHERE state->'_extra'->'voice'->>'call_sid' = "
                       f"'CA42' ORDER BY updated_at DESC LIMIT 1").fetchone()[0]
    session, messages, extra = api._load_session(conn, sid)
    conn.close()
    assert session.stats["blocked"] == {} and session.stats["writes"] == 1 and session.stats["proposals"] == 2
    assert [t.get("hedged") for t in extra["transcript"] if t["role"] == "user"] == [False, False, True, False, False]
    assert extra["transcript"][-3]["heard"] == "Done." and extra["transcript"][-2]["text"].startswith("(the caller")
    assert any("did not hear the rest" in str(m["content"]) for m in messages)
    assert len(extra["voice"]["turns"]) == 5 and all(t["filler"] for t in extra["voice"]["turns"])
    assert not extra["voice"]["busy"] and extra["voice"]["pending"] == []


def test_lambda_route_queues_while_busy(client, monkeypatch):
    """API Gateway can deliver a second utterance to another instance mid-turn: it is queued, not run twice."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(api.aws, "post_to_connection", lambda cid, m: sent.append((cid, m)))
    cid = f"c{uuid.uuid4().hex[:8]}="  # API Gateway connection ids look like this
    h = {"X-Voice-Secret": SECRET, "X-Connection-Id": cid}
    assert client.post("/voice/events", json={"type": "setup"}, headers={"X-Connection-Id": "x"}).status_code == 403
    assert client.post("/voice/events", json={"type": "setup", "callSid": "CA7"}, headers=h).status_code == 200
    conn = db.connect()
    session, messages, extra = api._load_session(conn, f"call_{cid}")
    extra["voice"]["busy"] = True  # a turn is running on another instance
    api._save_session(conn, session, messages, extra)
    assert client.post("/voice/events", json={"type": "prompt", "voicePrompt": "and my address?", "last": True},
                       headers=h).status_code == 200
    assert sent == []
    assert api._load_session(conn, f"call_{cid}")[2]["voice"]["pending"] == ["and my address?"]
    conn.close()
    assert client.post("/voice/connect?t=nope").status_code == 403 and client.post(
        f"/voice/connect?t={SECRET}").status_code == 200
