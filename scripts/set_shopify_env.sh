#!/usr/bin/env bash
# Stores the Shopify app credentials in .env (gitignored) without echoing the secret.
#   scripts/set_shopify_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
read -rp "Shopify store domain [order-ops-demo.myshopify.com]: " STORE
read -rp "Client ID: " ID
read -rsp "Client secret (hidden): " SEC; echo
[[ -n "$ID" && -n "$SEC" ]] || { echo "Client ID and secret are required" >&2; exit 1; }
touch .env
grep -vE '^SHOPIFY_(STORE|CLIENT_ID|CLIENT_SECRET)=' .env > .env.tmp || true
printf 'SHOPIFY_STORE=%s\nSHOPIFY_CLIENT_ID=%s\nSHOPIFY_CLIENT_SECRET=%s\n' \
  "${STORE:-order-ops-demo.myshopify.com}" "$ID" "$SEC" >> .env.tmp
mv .env.tmp .env && chmod 600 .env
echo "saved (secret length ${#SEC})"
