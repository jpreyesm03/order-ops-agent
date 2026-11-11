"""Tracing tests with an in-memory exporter: one trace per conversation, gate decisions on spans, no customer text."""
from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from order_ops import tracing
from order_ops.agent import Agent, finish_turn, traced_turn
from order_ops.gate import Gate, Session
from order_ops.router import RoutedAgent
from test_agent_loop import FakeClient, route, say, tool

EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    old = tracing._provider, tracing._tracer
    tracing.install(provider)
    EXPORTER.clear()
    yield EXPORTER
    tracing._provider, tracing._tracer = old


def _turn(agent, session, history, text, turn_no, ids):
    with tracing.in_conversation(ids), traced_turn(turn_no, "single", text) as sp:
        turn = agent.respond(session, history, text)
        finish_turn(sp, turn, agent.usage.cost(agent.model))
    return turn


def test_conversation_is_one_trace_across_requests(env, spans):
    conn, schema, oid, email = env
    gate, s, history = Gate(conn, schema), Session("ses_t1"), []
    ids = tracing.start_conversation(s.session_id, **{"order_ops.channel": "web"})
    client = FakeClient([tool("find_user_id_by_email", email=email), say("Confirm cancelling?"),
                         tool("cancel_pending_order", order_id=oid, reason="no longer needed"), say("Done.")])
    # two customer messages = two separate requests (a fresh Agent each time, as in the API)
    _turn(Agent(gate, client=client, model="claude-haiku-5-5"), s, history, f"cancel {oid}, I'm {email}", 1, ids)
    _turn(Agent(gate, client=client, model="claude-haiku-5-5"), s, history, "yes please", 2, ids)

    got = spans.get_finished_spans()
    assert {format(x.context.trace_id, "032x") for x in got} == {ids["trace_id"]}
    root = next(x for x in got if x.name == "conversation")
    turns = [x for x in got if x.name == "turn"]
    assert len(turns) == 2 and all(t.parent.span_id == root.context.span_id for t in turns)
    llm = [x for x in got if x.name == "llm"]
    assert len(llm) == 4 and all(x.attributes["gen_ai.usage.input_tokens"] == 10 for x in llm)
    assert all(x.attributes["order_ops.cost_usd"] > 0 for x in llm)
    tools = {x.attributes["tool.name"]: x for x in got if x.name == "tool"}
    write = tools["cancel_pending_order"]
    assert write.attributes["order_ops.outcome"] == "ok" and write.attributes["order_ops.tier"] == "write"
    assert write.attributes["order_ops.amount_usd"] > 0
    assert all(t.parent.span_id in {x.context.span_id for x in llm + turns} for t in tools.values())


def test_gate_block_is_on_the_span(env, spans):
    conn, schema, oid, email = env
    client = FakeClient([tool("find_user_id_by_email", email=email),
                         tool("cancel_pending_order", order_id=oid, reason="no longer needed"), say("ok")])
    Agent(Gate(conn, schema), client=client).respond(Session("ses_t2"), [], f"cancel {oid}, email {email}")
    blocked = next(x for x in spans.get_finished_spans() if x.attributes.get("tool.name") == "cancel_pending_order")
    assert blocked.attributes["order_ops.outcome"] == "blocked"
    assert blocked.attributes["order_ops.gate.rule"] == "confirmation"
    assert blocked.events[0].name == "gate.blocked" and "yes" in blocked.events[0].attributes["reason"]


def test_no_customer_text_or_personal_data_in_spans(env, spans, monkeypatch):
    conn, schema, oid, email = env
    monkeypatch.delenv("ORDER_OPS_TRACE_CONTENT", raising=False)
    s = Session("ses_t3")
    ids = tracing.start_conversation(s.session_id)
    client = FakeClient([tool("find_user_id_by_email", email=email), say("Found you, Noah.")])
    text = f"hi my email is {email}, please hurry"
    _turn(Agent(Gate(conn, schema), client=client), s, [], text, 1, ids)
    dumped = json.dumps([dict(x.attributes) for x in spans.get_finished_spans()], default=str)
    assert email not in dumped and "hurry" not in dumped and "Found you" not in dumped
    assert s.user_id not in dumped  # user ids embed the customer's name


def test_content_flag_adds_text_for_local_debugging(env, spans, monkeypatch):
    conn, schema, oid, email = env
    monkeypatch.setenv("ORDER_OPS_TRACE_CONTENT", "1")
    client = FakeClient([say("Hello!")])
    _turn(Agent(Gate(conn, schema), client=client), Session("ses_t4"), [], "hello there", 1, None)
    turn = next(x for x in spans.get_finished_spans() if x.name == "turn")
    assert turn.attributes["input.value"] == "hello there" and turn.attributes["output.value"] == "Hello!"


def test_router_span_records_choice(env, spans):
    conn, schema, oid, email = env
    client = FakeClient([route("account"), say("Hi, how can I help?")])
    RoutedAgent(Gate(conn, schema), client=client).respond(Session("ses_t5"), [], "hello")
    got = spans.get_finished_spans()
    router = next(x for x in got if x.name == "router")
    assert router.attributes["order_ops.router.choice"] == "account"
    assert next(x for x in got if x.name == "llm").attributes["order_ops.agent"] == "account"


def test_off_by_default_and_links(monkeypatch):
    monkeypatch.setattr(tracing, "_provider", None)
    assert tracing.start_conversation("ses_x") is None
    with tracing.in_conversation(None):
        pass
    ids = {"trace_id": "5759e988bd862e3fe1be46a994272793", "span_id": "53995c3f42cd8ad8"}
    assert tracing.xray_id(ids["trace_id"]) == "1-5759e988-bd862e3fe1be46a994272793"
    monkeypatch.delenv("ORDER_OPS_TRACE_URL", raising=False)
    assert tracing.trace_url(ids) is None
    monkeypatch.setenv("ORDER_OPS_TRACE_URL", "https://console.example/xray/{xray_id}?t={trace_id}")
    assert tracing.trace_url(ids) == ("https://console.example/xray/1-5759e988-bd862e3fe1be46a994272793"
                                      "?t=5759e988bd862e3fe1be46a994272793")


def test_console_links_ticket_to_its_trace(spans, monkeypatch):
    """API: the session opens the trace, the approval inbox links to it, the decision joins the same trace."""
    from fastapi.testclient import TestClient

    from order_ops import api, db

    monkeypatch.setenv("ORDER_OPS_TRACE_URL", "https://traces.example/{trace_id}")
    with TestClient(api.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        assert client.get(f"/api/sessions/{sid}").json()["trace_url"].startswith("https://traces.example/")
        conn = db.connect()
        oid, uid = conn.execute(f"""SELECT o.order_id, o.user_id FROM {api.LIVE}.orders o
                                    JOIN {api.LIVE}.order_payments p USING (order_id)
                                    WHERE o.status = 'pending' AND p.amount > 300
                                    ORDER BY random() LIMIT 1""").fetchone()
        gate, s = Gate(conn, api.LIVE), Session(sid, approval_threshold=250)
        gate.run(s, "find_user_id_by_email", {"email": db.get_user(conn, api.LIVE, uid)["email"]})
        s.on_customer_message("yes, cancel it")
        aid = gate.run(s, "cancel_pending_order", {"order_id": oid, "reason": "no longer needed"})["approval_id"]
        conn.close()
        ticket = next(a for a in client.get("/api/approvals").json() if a["approval_id"] == aid)
        assert ticket["trace_url"] == f"https://traces.example/{ticket['trace_id']}"
        client.post(f"/api/approvals/{aid}/decision", json={"approve": False, "reviewer": "tester"})
    decision = next(x for x in spans.get_finished_spans() if x.name == "approval.decision")
    assert format(decision.context.trace_id, "032x") == ticket["trace_id"]
    assert decision.attributes["order_ops.approval.status"] == "rejected"
