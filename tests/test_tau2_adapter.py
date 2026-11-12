"""The agent inside τ²-bench's own harness, with scripted model and customer: no API calls (D21–D23)."""
# ruff: noqa: I001, E402  (the adapter must be imported before any tau2 module)
from __future__ import annotations

import json

import pytest

# Import the adapter first: it points TAU2_DATA_DIR at this repo's data before τ² reads it.
adapter = pytest.importorskip("order_ops.tau2_adapter", reason="needs the eval group: uv sync --group tau2")
P5Tau2Agent = adapter.P5Tau2Agent
from tau2.data_model.message import AssistantMessage, UserMessage  # noqa: E402
from tau2.evaluator.evaluator import EvaluationType  # noqa: E402
from tau2.orchestrator.orchestrator import Orchestrator  # noqa: E402
from tau2.runner import build_environment, get_tasks, run_simulation  # noqa: E402
from tau2.user.user_simulator_base import STOP, HalfDuplexUser, UserState  # noqa: E402
from test_agent_loop import FakeClient, route, say, tool  # noqa: E402

from order_ops.gate import GATE_TOOLS  # noqa: E402
from order_ops.tool_specs import TOOLS  # noqa: E402

TASK, ORDER, EMAIL = "38", "#W9348897", "daiki.sanchez1479@example.com"  # task 38: one cancellation


def _agent(script: list, **kw) -> tuple[P5Tau2Agent, FakeClient, object]:
    env = build_environment("retail")
    client = FakeClient(script)
    agent = P5Tau2Agent(env.get_tools(), env.get_policy(), client=client, model="claude-haiku-5-5", **kw)
    return agent, client, env


def _drive(agent: P5Tau2Agent, env, text: str) -> AssistantMessage:
    """What τ²'s orchestrator does for one customer message: run every tool call the agent emits."""
    out, state = agent.generate_next_message(UserMessage(role="user", content=text), {})
    while out.tool_calls:
        out, state = agent.generate_next_message(env.get_response(out.tool_calls[0]), state)
    return out


def test_tool_specs_match_tau2():
    """The app's tool definitions are the ones τ² executes: same names and required arguments."""
    env = build_environment("retail")
    theirs = {t.name: set(t.openai_schema["function"]["parameters"].get("required", [])) for t in env.get_tools()}
    ours = {t["name"]: set(t["input_schema"].get("required", [])) for t in TOOLS if t["name"] not in GATE_TOOLS}
    assert ours == theirs


def test_gate_on_proposes_unconfirmed_write_and_tau2_never_sees_it():
    agent, client, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                                 tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"),
                                 say("Please confirm you want to cancel.")])
    reply = _drive(agent, env, f"cancel {ORDER}, my email is {EMAIL}")
    assert reply.content.startswith("Please confirm you want to cancel.") and ORDER in reply.content  # + summary
    assert env.tools.db.orders[ORDER].status == "pending"
    rep = agent.report()
    assert rep["gate_stats"]["blocked"] == {} and rep["gate_stats"]["proposals"] == 1
    assert [c["ran_in_tau2"] for c in rep["tool_calls"]] == [True, False]
    proposal = json.loads(client.requests[2]["messages"][-1]["content"][0]["content"])  # what the model saw
    assert proposal["needs_confirmation"] and ORDER in proposal["summary"]


def test_invented_address_is_blocked():
    agent, _, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                            tool("modify_pending_order_address", order_id=ORDER, address1="1 Invented St",
                                 address2="", city="Nowhere", state="ZZ", country="USA", zip="00000"),
                            say("I need your address.")])
    _drive(agent, env, f"change the address of {ORDER}, I'm {EMAIL}")
    assert agent.report()["gate_stats"]["blocked"] == {"provenance": 1}


def test_find_variants_is_answered_locally():
    product = next(iter(build_environment("retail").tools.db.products.values()))
    opt = next(iter(next(iter(product.variants.values())).options.items()))
    agent, _, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                            tool("find_variants", product_id=product.product_id, wanted_options={opt[0]: opt[1]}),
                            say("Here are the options.")])
    _drive(agent, env, f"what variants are there? I'm {EMAIL}")
    call = agent.report()["tool_calls"][1]
    assert call["ran_in_tau2"] is False and call["result"]["result"]["best_matches"][0]["matches"] == 1


def test_gate_off_lets_it_through_and_records_what_it_would_have_blocked():
    agent, _, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                            tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"),
                            say("Cancelled.")], gate="off")
    _drive(agent, env, f"cancel {ORDER}, my email is {EMAIL}")
    assert env.tools.db.orders[ORDER].status == "cancelled"
    stats = agent.report()["gate_stats"]
    assert stats["blocked"] == {} and stats["would_block"] == {"confirmation": 1}


def test_ownership_uses_tau2_database():
    other = next(o for o in build_environment("retail").tools.db.orders.values() if o.user_id != "daiki_sanchez_3253")
    agent, _, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                            tool("get_order_details", order_id=other.order_id), say("Sorry, that's not yours.")])
    _drive(agent, env, f"what's in {other.order_id}? I'm {EMAIL}")
    assert agent.report()["gate_stats"]["blocked"] == {"ownership": 1}


def test_one_yes_pays_for_one_write_and_duplicates_are_stopped():
    agent, _, env = _agent([tool("find_user_id_by_email", email=EMAIL),
                            tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"), say("Confirm?"),
                            tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"),
                            tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"), say("Done.")])
    _drive(agent, env, f"cancel {ORDER}, I'm {EMAIL}")
    _drive(agent, env, "yes")
    stats = agent.report()["gate_stats"]
    assert stats["writes"] == 1 and stats["approvals"] == 1  # $1,166.98 is above the $250 threshold
    assert stats["duplicates"] == 1  # the repeated call: answered as already done, not run again


def test_routed_handoff_and_specialist_tools():
    agent, client, env = _agent([route("account"), tool("handoff", to="orders", reason="cancellation"),
                                 tool("find_user_id_by_email", email=EMAIL), say("Which order?")], arch="routed")
    reply = _drive(agent, env, f"I need to cancel something, I'm {EMAIL}")
    rep = agent.report()
    assert reply.content == "Which order?" and rep["routes"] == ["account"] and rep["handoffs"] == 1
    assert agent.session.user_id == "daiki_sanchez_3253"
    orders_tools = {t["name"] for t in client.requests[2]["tools"]}
    assert "cancel_pending_order" in orders_tools and "return_delivered_order_items" not in orders_tools
    assert "colleague" not in client.requests[2]["messages"][0]["content"]  # nothing to recap yet


class ScriptedUser(HalfDuplexUser[UserState]):
    def __init__(self, lines: list[str]):
        super().__init__(instructions="scripted")
        self.lines = list(lines)

    def get_init_state(self, message_history=None) -> UserState:
        return UserState(system_messages=[], messages=list(message_history or []))

    def generate_next_message(self, message, state):
        msg = UserMessage(role="user", content=self.lines.pop(0))
        state.messages.append(msg)
        return msg, state

    @classmethod
    def is_stop(cls, message: UserMessage) -> bool:
        return STOP in (message.content or "")

    def set_seed(self, seed: int) -> None:
        pass


@pytest.mark.parametrize("gate", ["on", "off"])
def test_full_tau2_episode_is_graded_by_tau2(gate):
    """τ²'s orchestrator + evaluator on task 38. The blocked early attempt is invisible to τ²; the DB check passes."""
    task = get_tasks("retail", task_ids=[TASK])[0]
    env = build_environment("retail")
    client = FakeClient([tool("find_user_id_by_email", email=EMAIL),
                         tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"),  # no yes yet
                         say("To confirm: cancel #W9348897, reason 'no longer needed'. Yes?"),
                         tool("cancel_pending_order", order_id=ORDER, reason="no longer needed"), say("Done.")])
    agent = P5Tau2Agent(env.get_tools(), env.get_policy(), client=client, model="claude-haiku-5-5", gate=gate)
    user = ScriptedUser([f"Hi, cancel order {ORDER} please, my email is {EMAIL}", "yes", STOP])
    orch = Orchestrator(domain="retail", agent=agent, user=user, environment=env, task=task, max_steps=40)
    sim = run_simulation(orch, evaluation_type=EvaluationType.ENV)
    names = [tc.name for m in sim.messages if getattr(m, "tool_calls", None) for tc in m.tool_calls]
    if gate == "on":
        assert names == ["find_user_id_by_email", "cancel_pending_order"]  # the blocked try never reached τ²
        assert sim.reward_info.reward == 1.0
    else:  # without the gate the unconfirmed cancel runs; the second one then fails (already cancelled)
        assert names == ["find_user_id_by_email", "cancel_pending_order", "cancel_pending_order"]
        assert agent.report()["gate_stats"]["would_block"] == {"confirmation": 1}
