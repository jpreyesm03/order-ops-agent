"""Agent-loop tests with a scripted fake model: no API calls, deterministic."""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from order_ops import db
from order_ops.agent import Agent
from order_ops.gate import Gate, Session
from order_ops.router import RoutedAgent


@dataclass
class Block:
    type: str
    text: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: "tu_" + uuid.uuid4().hex[:8])

    def model_dump(self, exclude_none: bool = True) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


def tool(name: str, **args) -> SimpleNamespace:
    return SimpleNamespace(content=[Block("tool_use", name=name, input=args)], stop_reason="tool_use", usage=_U)


def say(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[Block("text", text=text)], stop_reason="end_turn", usage=_U)


def route(to: str) -> SimpleNamespace:
    return SimpleNamespace(content=[Block("text", text=json.dumps({"specialist": to}))], stop_reason="end_turn",
                           usage=_U)


_U = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeClient:
    """Plays back scripted responses in order and records every request."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        return self.script.pop(0)


@pytest.fixture
def env():
    conn = db.connect()
    have = conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='base'").fetchone()
    if not have:
        db.load_store(conn, "base", db.load_tau2_db())
    schema = f"t_{uuid.uuid4().hex[:8]}"
    db.clone_schema(conn, "base", schema)
    oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {schema}.orders o JOIN {schema}.order_payments p
                                USING (order_id) WHERE o.status='pending' AND p.amount < 200
                                ORDER BY o.order_id LIMIT 1""").fetchone()
    email = db.get_user(conn, schema, uid)["email"]
    yield conn, schema, oid, email
    db.drop_schema(conn, schema)
    conn.close()


def test_agent_cancels_after_yes(env):
    conn, schema, oid, email = env
    gate = Gate(conn, schema)
    s, history = Session("a1"), []
    client = FakeClient([tool("find_user_id_by_email", email=email), say("Confirm cancelling?"),
                         tool("cancel_pending_order", order_id=oid, reason="no longer needed"), say("Done.")])
    agent = Agent(gate, client=client, model="claude-haiku-5-5")
    agent.respond(s, history, f"cancel {oid}, my email is {email}")
    turn = agent.respond(s, history, "yes please")
    assert turn.reply == "Done." and turn.tool_calls[0]["result"]["ok"]
    assert db.get_order(conn, schema, oid)["status"] == "cancelled"
    # tool results go back as tool_result blocks with the matching id
    assert history[-2]["content"][0]["type"] == "tool_result"


def test_model_cannot_skip_confirmation(env):
    conn, schema, oid, email = env
    gate = Gate(conn, schema)
    client = FakeClient([tool("find_user_id_by_email", email=email),
                         tool("cancel_pending_order", order_id=oid, reason="no longer needed"), say("ok")])
    turn = Agent(gate, client=client).respond(Session("a2"), [], f"cancel {oid}, email {email}")
    assert turn.tool_calls[1]["result"]["blocked_by_policy"] == "confirmation"
    assert db.get_order(conn, schema, oid)["status"] == "pending"


def test_router_recap_yes_is_not_a_confirmation(env):
    """'yes' from an earlier turn shows up in the recap given to a new specialist; it must not count."""
    conn, schema, oid, email = env
    gate = Gate(conn, schema)
    client = FakeClient([
        route("account"), tool("find_user_id_by_email", email=email), say("Anything else?"),
        route("orders"), tool("cancel_pending_order", order_id=oid, reason="no longer needed"), say("hmm"),
    ])
    agent = RoutedAgent(gate, client=client)
    s = Session("r1")
    agent.respond(s, [], f"yes, my email is {email}")      # turn 1: a 'yes' that is about something else
    turn = agent.respond(s, [], f"what about order {oid}?")  # turn 2: new specialist sees turn 1 in its recap
    assert "colleague" in client.requests[4]["messages"][-1]["content"]  # the recap was passed in
    assert turn.tool_calls[0]["result"]["blocked_by_policy"] == "confirmation"


def test_handoff_does_not_reuse_the_yes(env):
    """A 'yes' consumed before a handoff can't pay for a second change after the handoff."""
    conn, schema, oid, email = env
    gate = Gate(conn, schema)
    s = Session("r2", approval_threshold=1e9)
    gate.run(s, "find_user_id_by_email", {"email": email})
    client = FakeClient([
        route("orders"),
        tool("cancel_pending_order", order_id=oid, reason="no longer needed"),
        tool("handoff", to="account", reason="address"),
        tool("modify_user_address", user_id=s.user_id, address1="1 A St", address2="", city="X", state="WA",
             country="USA", zip="98105"),
        say("done"),
    ])
    turn = RoutedAgent(gate, client=client).respond(s, [], "yes")
    assert turn.tool_calls[0]["result"]["ok"]
    assert turn.tool_calls[1]["result"]["blocked_by_policy"] == "confirmation"
