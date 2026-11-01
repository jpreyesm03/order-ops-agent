"""Subscribe the app to the Shopify events that keep the mirror in sync (idempotent).

    uv run python -m order_ops.shopify_webhooks https://<api-host>/webhooks/shopify
"""
from __future__ import annotations

import sys

from . import db
from .shopify import ShopifyClient

TOPICS = ["ORDERS_CANCELLED", "ORDERS_UPDATED", "ORDERS_EDITED", "REFUNDS_CREATE", "RETURNS_REQUEST",
          "RETURNS_APPROVE", "RETURNS_CLOSE"]

LIST = """{ webhookSubscriptions(first: 50) { nodes { id topic uri } } }"""
CREATE = """mutation($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: {uri: $uri, format: JSON}) {
    webhookSubscription { id } userErrors { field message } } }"""
DELETE = """mutation($id: ID!) { webhookSubscriptionDelete(id: $id) { deletedWebhookSubscriptionId
  userErrors { field message } } }"""


def main() -> None:
    uri = sys.argv[1]
    db.connect().close()  # loads .env
    shop = ShopifyClient()
    existing = shop.gql(LIST)["webhookSubscriptions"]["nodes"]
    for sub in existing:
        if sub["uri"] != uri:  # endpoint moved: drop the old subscription
            shop.mutate(DELETE, {"id": sub["id"]}, "webhookSubscriptionDelete")
            print(f"removed {sub['topic']} -> {sub['uri']}")
    have = {s["topic"] for s in existing if s["uri"] == uri}
    for topic in TOPICS:
        if topic not in have:
            shop.mutate(CREATE, {"topic": topic, "uri": uri}, "webhookSubscriptionCreate")
        print(f"{topic} -> {uri}")


if __name__ == "__main__":
    main()
