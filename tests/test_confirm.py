"""Confirmation summaries, fingerprints, provenance and variant ranking (D31)."""
from __future__ import annotations

from order_ops import confirm

ORDER = {"order_id": "#W1", "status": "delivered", "items": [
    {"item_id": "k1", "name": "Mechanical Keyboard", "price": 272.33,
     "options": {"switch type": "linear", "backlight": "RGB"}},
    {"item_id": "t1", "name": "Smart Thermostat", "price": 100.0, "options": {"compatibility": "Apple HomeKit"}}]}
VARIANTS = {"k2": {"item_id": "k2", "options": {"switch type": "clicky", "backlight": "none"}, "price": 269.16}}


def test_fingerprint_ignores_pair_order_but_not_content():
    a = {"order_id": "#W1", "item_ids": ["k1", "t1"], "new_item_ids": ["k2", "t2"], "payment_method_id": "pm"}
    b = {"order_id": "#W1", "item_ids": ["t1", "k1"], "new_item_ids": ["t2", "k2"], "payment_method_id": "pm"}
    c = {"order_id": "#W1", "item_ids": ["k1", "t1"], "new_item_ids": ["t2", "k2"], "payment_method_id": "pm"}
    f = confirm.fingerprint
    assert f("exchange_delivered_order_items", a) == f("exchange_delivered_order_items", b)
    assert f("exchange_delivered_order_items", a) != f("exchange_delivered_order_items", c)
    assert f("exchange_delivered_order_items", a) != f("modify_pending_order_items", a)


def test_summary_shows_what_runs_and_what_it_rules_out():
    text = confirm.summary("exchange_delivered_order_items",
                           {"order_id": "#W1", "item_ids": ["k1"], "new_item_ids": ["k2"], "payment_method_id": "pm"},
                           ORDER, VARIANTS.get)
    assert "switch type linear, backlight RGB $272.33 → switch type clicky, backlight none $269.16" in text
    assert "refund $3.17 using pm" in text
    assert "NOT included: Smart Thermostat (compatibility Apple HomeKit)" in text
    assert "can't be returned, exchanged or changed again" in text


def test_summary_flags_exchange_to_the_same_item():
    text = confirm.summary("exchange_delivered_order_items",
                           {"order_id": "#W1", "item_ids": ["k1"], "new_item_ids": ["k1"], "payment_method_id": "pm"},
                           ORDER, lambda i: {"options": ORDER["items"][0]["options"], "price": 272.33})
    assert "(same item!)" in text


def test_provenance_accepts_seen_values_and_names_the_invented_ones():
    seen = ('{"address": {"address1": "565 Maple Drive", "address2": "Suite 501", "zip": "20307"}} '
            'user: my city is Washington DC')
    args = {"address1": "565 Maple Dr", "address2": "Suite 501", "city": "Washington", "state": "DC",
            "country": "USA", "zip": "20500"}
    missing = confirm.unsourced(args, seen + " USA")
    assert missing == ["zip '20500'"]
    assert confirm.unsourced({"item_ids": ["k1"], "payment_method_id": "pm_9"}, '"k1" "pm_9"') == []
    assert confirm.unsourced({"item_ids": ["k10"]}, '"k1"') == ["item_ids 'k10'"]  # no partial-id matches


def test_rank_variants_lists_every_option_value():
    product = {"product_id": "p", "name": "Smart Thermostat", "variants": {
        "a": {"options": {"compatibility": "Google Assistant", "color": "black"}, "available": True, "price": 1},
        "b": {"options": {"compatibility": "Apple HomeKit", "color": "black"}, "available": True, "price": 2},
        "c": {"options": {"compatibility": "Amazon Alexa", "color": "white"}, "available": False, "price": 3}}}
    out = confirm.rank_variants(product, {"compatibility": "Google Home", "color": "black"})
    assert out["option_values"]["compatibility"] == ["Amazon Alexa", "Apple HomeKit", "Google Assistant"]
    assert out["best_matches"][0]["matches"] == 1 and "compatibility" in out["best_matches"][0]["differs_on"]


def test_done_line_names_the_change_and_the_target():
    assert confirm.done("modify_user_address", {"user_id": "u1"}) == "Done: Default address of account u1 changed."
    assert confirm.done("return_delivered_order_items", {"order_id": "#W1"}).startswith("Done: Return requested")
    assert confirm.done("cancel_pending_order", {"order_id": "#W1"}, pending_approval=True).startswith(
        "Submitted for supervisor approval")


def test_session_hands_out_done_lines_then_summary_once():
    from order_ops.gate import Session
    s = Session(session_id="x")
    s.note_done("modify_user_address", {"user_id": "u1"})
    s.new_summary = "Please confirm this change."
    assert s.take_summary() == "Done: Default address of account u1 changed.\n\nPlease confirm this change."
    assert s.take_summary() is None


def test_summary_warns_about_mentioned_items_left_out_of_a_one_shot_change():
    order = {"status": "delivered", "items": [
        {"item_id": "a", "name": "Bookshelf", "options": {}, "price": 10},
        {"item_id": "b", "name": "Jigsaw Puzzle", "options": {}, "price": 5}]}
    args = {"order_id": "#W1", "item_ids": ["a"], "payment_method_id": "pm"}
    said = "I want to return both bookshelves and both jigsaw puzzles."
    assert confirm.left_out("return_delivered_order_items", args, order, said) == ["Jigsaw Puzzle"]
    text = confirm.summary("return_delivered_order_items", args, order, lambda _: None, said=said)
    assert "Warning: you mentioned Jigsaw Puzzle" in text
    assert confirm.left_out("return_delivered_order_items", args, order, "just the bookshelf") == []
