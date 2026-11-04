#!/usr/bin/env bash
# Creates the API's Lambda function (container image + `live` alias + public Function URL on the alias + nightly
# reseed), or updates it to match this file, then releases $LATEST through the smoke-checked alias (same flow as
# CI). This file is the source of truth for the API's runtime config. Code deploys happen in CI.
#   scripts/lambda_service.sh                          # create / update config (image: :latest on first create)
#   IMAGE_TAG=<git sha> scripts/lambda_service.sh      # also deploy a specific build
#   CORS_ORIGINS=https://x.vercel.app scripts/lambda_service.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

FUNCTION="$APP-api"
ALIAS=live
LOG_GROUP="/aws/lambda/$FUNCTION"
ROLE="arn:aws:iam::$ACCOUNT_ID:role/$APP-lambda"
CORS_ORIGINS="${CORS_ORIGINS:-https://order-ops-agent.vercel.app,http://localhost:3005}"
TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
exists() { "$@" >/dev/null 2>&1; }

# Non-secret config. Secrets are read from SSM (SSM_PARAMETER_PATH) at cold start, so they never sit in the
# function's configuration.
python3 - "$CORS_ORIGINS" > "$TMP/env.json" <<'EOF'
import json, sys
print(json.dumps({"Variables": {
    "CORS_ORIGINS": sys.argv[1], "APPROVAL_THRESHOLD_USD": "250", "AGENT_MODEL": "claude-haiku-5-5",
    "AGENT_EFFORT": "medium", "MAX_TURNS_PER_SESSION": "40", "TURNS_PER_IP_PER_HOUR": "60", "TURNS_PER_DAY": "1500",
    "STORE_BACKEND": "shopify", "SHOPIFY_STORE": "order-ops-demo.myshopify.com", "SHOPIFY_SEED_USERS": "8",
    "SSM_PARAMETER_PATH": "/order-ops/", "LAMBDA_ALIAS": "live"}}))
EOF
# 512 MB: the agent mostly waits on the model and Shopify. 900 s: a chat turn takes seconds, the reseed about a
# minute; the Function URL allows up to the function timeout (no 29 s API Gateway cap).
CONFIG=(--role "$ROLE" --timeout 900 --memory-size 512 --environment "file://$TMP/env.json")

aws logs create-log-group --log-group-name "$LOG_GROUP" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 14

if exists aws lambda get-function --function-name "$FUNCTION"; then
  aws lambda update-function-configuration --function-name "$FUNCTION" "${CONFIG[@]}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION"
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    aws lambda update-function-code --function-name "$FUNCTION" --image-uri "$ECR_URI:$IMAGE_TAG" >/dev/null
    aws lambda wait function-updated-v2 --function-name "$FUNCTION"
  fi
  echo "lambda: updated $FUNCTION (\$LATEST)"
else
  aws lambda create-function --function-name "$FUNCTION" --package-type Image --architectures x86_64 \
    --code "ImageUri=$ECR_URI:${IMAGE_TAG:-latest}" "${CONFIG[@]}" >/dev/null
  aws lambda wait function-active-v2 --function-name "$FUNCTION"
  echo "lambda: created $FUNCTION"
fi

# Release: freeze $LATEST as a numbered version, check that version directly, and only then point the `live`
# alias (which all traffic uses) at it. A version that fails the check never receives a request.
VERSION="$(aws lambda publish-version --function-name "$FUNCTION" --query Version --output text)"
scripts/lambda_smoke.sh "$VERSION"
if exists aws lambda get-alias --function-name "$FUNCTION" --name "$ALIAS"; then
  PREVIOUS="$(aws lambda get-alias --function-name "$FUNCTION" --name "$ALIAS" --query FunctionVersion --output text)"
  aws lambda update-alias --function-name "$FUNCTION" --name "$ALIAS" --function-version "$VERSION" >/dev/null
  echo "lambda: $ALIAS $PREVIOUS -> $VERSION (rollback: scripts/lambda_rollback.sh $PREVIOUS)"
else
  aws lambda create-alias --function-name "$FUNCTION" --name "$ALIAS" --function-version "$VERSION" >/dev/null
  echo "lambda: $ALIAS -> $VERSION"
fi

# Async invocations (reseed, schedule) go to the alias: no automatic retries. A failed reseed is logged and the
# next one runs on schedule; a retry racing a half-done reseed would be worse.
aws lambda put-function-event-invoke-config --function-name "$FUNCTION" --qualifier "$ALIAS" \
  --maximum-retry-attempts 0 --maximum-event-age-in-seconds 3600 >/dev/null

# Public HTTPS endpoint on the alias. Auth NONE like the load balancer it replaces: the app does its own checks
# (rate limits, admin token, Shopify HMAC). CORS stays in the app (one source of truth, no duplicate headers).
if ! exists aws lambda get-function-url-config --function-name "$FUNCTION" --qualifier "$ALIAS"; then
  aws lambda create-function-url-config --function-name "$FUNCTION" --qualifier "$ALIAS" --auth-type NONE \
    --invoke-mode BUFFERED >/dev/null
  aws lambda add-permission --function-name "$FUNCTION" --qualifier "$ALIAS" --statement-id public-url \
    --principal "*" --action lambda:InvokeFunctionUrl --function-url-auth-type NONE >/dev/null
  aws lambda add-permission --function-name "$FUNCTION" --qualifier "$ALIAS" --statement-id public-url-invoke \
    --principal "*" --action lambda:InvokeFunction --invoked-via-function-url >/dev/null
fi
URL="$(aws lambda get-function-url-config --function-name "$FUNCTION" --qualifier "$ALIAS" --query FunctionUrl \
  --output text)"

# Nightly reseed (EventBridge Scheduler → async invoke → POST /events). 10:00 UTC = 2–3 am in Seattle.
python3 - "arn:aws:lambda:$AWS_REGION:$ACCOUNT_ID:function:$FUNCTION:$ALIAS" \
  "arn:aws:iam::$ACCOUNT_ID:role/$APP-scheduler" \
  "$(aws ssm get-parameter --name "/$APP/ADMIN_TOKEN" --with-decryption --query Parameter.Value --output text)" \
  > "$TMP/target.json" <<'EOF'
import json, sys
fn, role, token = sys.argv[1:]
print(json.dumps({"Arn": fn, "RoleArn": role, "Input": json.dumps({"task": "reseed", "token": token}),
                  "RetryPolicy": {"MaximumRetryAttempts": 0}}))
EOF
SCHEDULE=(--name "$APP-nightly-reseed" --schedule-expression "cron(0 10 * * ? *)" --flexible-time-window Mode=OFF
          --target "file://$TMP/target.json" --state ENABLED)
if exists aws scheduler get-schedule --name "$APP-nightly-reseed"; then
  aws scheduler update-schedule "${SCHEDULE[@]}" >/dev/null
else
  aws scheduler create-schedule "${SCHEDULE[@]}" >/dev/null
fi
echo "scheduler: $APP-nightly-reseed (daily 10:00 UTC)"
echo "lambda: endpoint ${URL%/}"
