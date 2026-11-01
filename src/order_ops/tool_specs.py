"""Tool definitions shown to the model (JSON Schema). Descriptions follow the τ²-bench retail tool docs."""
from __future__ import annotations

_ORDER_ID = {"type": "string", "description": "The order id, such as '#W0000000'. Note the '#' at the start."}
_PM_ID = {"type": "string", "description": "Payment method id, such as 'gift_card_0000000' or 'credit_card_0000000'. "
                                           "Look it up in the user or order details."}
_ITEM_LIST = {"type": "array", "items": {"type": "string"}}
_ADDRESS = {
    "address1": {"type": "string", "description": "First line, such as '123 Main St'."},
    "address2": {"type": "string", "description": "Second line, such as 'Apt 1' or ''."},
    "city": {"type": "string"},
    "state": {"type": "string", "description": "Such as 'CA'."},
    "country": {"type": "string", "description": "Such as 'USA'."},
    "zip": {"type": "string", "description": "Such as '12345'."},
}
_ADDR_KEYS = ["address1", "address2", "city", "state", "country", "zip"]


def _tool(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": required or list(props),
                             "additionalProperties": False}}


TOOLS: list[dict] = [
    _tool("calculate", "Calculate a math expression with numbers, + - * / and parentheses, e.g. '2 + 2'.",
          {"expression": {"type": "string"}}),
    _tool("cancel_pending_order",
          "Cancel a pending order (not processed or delivered). Explain the details and get explicit customer "
          "confirmation first. Status becomes 'cancelled'; refunds go to gift cards immediately, otherwise in 5-7 "
          "business days.",
          {"order_id": _ORDER_ID,
           "reason": {"type": "string", "enum": ["no longer needed", "ordered by mistake"]}}),
    _tool("exchange_delivered_order_items",
          "Exchange items of a delivered order for available items of the same product with different options. "
          "Can only be done once per order, so collect every item first. Explain the details and get explicit "
          "confirmation first.",
          {"order_id": _ORDER_ID,
           "item_ids": {**_ITEM_LIST, "description": "Item ids to exchange (duplicates allowed)."},
           "new_item_ids": {**_ITEM_LIST, "description": "New item ids, same position and product as item_ids."},
           "payment_method_id": _PM_ID}),
    _tool("find_user_id_by_email", "Find a user id by email. Use this first to authenticate the customer.",
          {"email": {"type": "string"}}),
    _tool("find_user_id_by_name_zip",
          "Find a user id by first name, last name and zip code. Use only if the customer can't give an email "
          "or it is not found.",
          {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "zip": {"type": "string"}}),
    _tool("get_order_details", "Get the status and details of an order.", {"order_id": _ORDER_ID}),
    _tool("get_product_details",
          "Get a product's variants (item ids, options, availability, price). Product id is NOT an item id.",
          {"product_id": {"type": "string"}}),
    _tool("get_item_details", "Get one item (variant) by item id. Item id is NOT a product id.",
          {"item_id": {"type": "string"}}),
    _tool("get_user_details", "Get a user's profile, payment methods and order ids.",
          {"user_id": {"type": "string", "description": "Such as 'sara_doe_496'."}}),
    _tool("list_all_product_types", "List the name and product id of all 50 product types.", {}),
    _tool("modify_pending_order_address",
          "Change the shipping address of a pending order. Get explicit confirmation first.",
          {"order_id": _ORDER_ID, **_ADDRESS}, ["order_id", *_ADDR_KEYS]),
    _tool("modify_pending_order_items",
          "Swap items in a pending order for available items of the same product with different options. Can only "
          "be called once per order (status becomes 'pending (item modified)'), so collect every item first and "
          "get explicit confirmation.",
          {"order_id": _ORDER_ID,
           "item_ids": {**_ITEM_LIST, "description": "Item ids to modify (duplicates allowed)."},
           "new_item_ids": {**_ITEM_LIST, "description": "New item ids, same position and product as item_ids."},
           "payment_method_id": _PM_ID}),
    _tool("modify_pending_order_payment",
          "Change the payment method of a pending order. Get explicit confirmation first.",
          {"order_id": _ORDER_ID, "payment_method_id": _PM_ID}),
    _tool("modify_user_address", "Change a user's default address. Get explicit confirmation first.",
          {"user_id": {"type": "string"}, **_ADDRESS}, ["user_id", *_ADDR_KEYS]),
    _tool("return_delivered_order_items",
          "Return items of a delivered order. Refund goes to the original payment method or an existing gift card. "
          "Status becomes 'return requested'. Get explicit confirmation first.",
          {"order_id": _ORDER_ID,
           "item_ids": {**_ITEM_LIST, "description": "Item ids to return (duplicates allowed)."},
           "payment_method_id": _PM_ID}),
    _tool("transfer_to_human_agents",
          "Transfer to a human agent with a summary. Only if the customer explicitly asks for a human, or the "
          "request cannot be handled with the policy and tools.",
          {"summary": {"type": "string"}}),
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
