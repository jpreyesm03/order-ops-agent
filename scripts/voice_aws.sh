#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the voice channel (D26): an API Gateway WebSocket API that Twilio
# ConversationRelay connects to. It forwards every message to the API's Lambda (Function URL):
#   $connect  -> POST /voice/connect?t=<secret>   (accepts the socket only with the secret from our TwiML)
#   $default  -> POST /voice/events               (X-Connection-Id + X-Voice-Secret headers added here)
# Replies go back through the API Gateway management API, so the Lambda role may post to these connections.
#
# Before: a Twilio account + phone number. After:
#   1. aws ssm put-parameter --name /order-ops/TWILIO_AUTH_TOKEN --type SecureString --value <token>
#   2. scripts/lambda_service.sh   (new cold start reads the new SSM parameters)
#   3. Twilio console → the number → "A call comes in": Webhook, POST, <Function URL>/voice/incoming
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

FUNCTION="$APP-api"
NAME="$APP-voice"
STAGE=live
SSM=/order-ops
FN_URL="$(aws lambda get-function-url-config --function-name "$FUNCTION" --qualifier live --query FunctionUrl \
  --output text)"
FN_URL="${FN_URL%/}"

API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='$NAME'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  # route on a field Twilio never sends, so every message takes $default
  API_ID="$(aws apigatewayv2 create-api --name "$NAME" --protocol-type WEBSOCKET \
    --route-selection-expression '$request.body.order_ops_route' --query ApiId --output text)"
  echo "apigw: created $NAME ($API_ID)"
fi

if ! aws ssm get-parameter --name "$SSM/VOICE_RELAY_SECRET" >/dev/null 2>&1; then
  aws ssm put-parameter --name "$SSM/VOICE_RELAY_SECRET" --type SecureString \
    --value "$(openssl rand -hex 24)" >/dev/null
  echo "ssm: generated VOICE_RELAY_SECRET"
fi
SECRET="$(aws ssm get-parameter --name "$SSM/VOICE_RELAY_SECRET" --with-decryption --query Parameter.Value \
  --output text)"

integration() {  # $1 route key, $2 path, $3 request parameters (JSON)
  local id
  id="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?IntegrationUri=='$FN_URL$2'].IntegrationId | [0]" --output text)"
  if [[ "$id" == "None" || -z "$id" ]]; then
    id="$(aws apigatewayv2 create-integration --api-id "$API_ID" --integration-type HTTP_PROXY \
      --integration-method POST --integration-uri "$FN_URL$2" --request-parameters "$3" \
      --query IntegrationId --output text)"
  else
    aws apigatewayv2 update-integration --api-id "$API_ID" --integration-id "$id" --request-parameters "$3" >/dev/null
  fi
  local route
  route="$(aws apigatewayv2 get-routes --api-id "$API_ID" --query "Items[?RouteKey=='$1'].RouteId | [0]" \
    --output text)"
  if [[ "$route" == "None" || -z "$route" ]]; then
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$1" --target "integrations/$id" >/dev/null
  fi
  echo "apigw: $1 -> $2"
}
integration '$connect' /voice/connect '{"integration.request.querystring.t": "route.request.querystring.t"}'
integration '$default' /voice/events "{\"integration.request.header.X-Connection-Id\": \"context.connectionId\",
  \"integration.request.header.X-Voice-Secret\": \"'$SECRET'\"}"

if ! aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name "$STAGE" >/dev/null 2>&1; then
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name "$STAGE" --auto-deploy >/dev/null
fi

WS_URL="wss://$API_ID.execute-api.$AWS_REGION.amazonaws.com/$STAGE"
CALLBACK_URL="https://$API_ID.execute-api.$AWS_REGION.amazonaws.com/$STAGE"
for kv in "VOICE_WS_URL=$WS_URL" "VOICE_WS_CALLBACK_URL=$CALLBACK_URL" "VOICE_PUBLIC_URL=$FN_URL"; do
  aws ssm put-parameter --name "$SSM/${kv%%=*}" --type String --value "${kv#*=}" --overwrite >/dev/null
done
echo "ssm: VOICE_WS_URL, VOICE_WS_CALLBACK_URL, VOICE_PUBLIC_URL"

aws iam put-role-policy --role-name "$APP-lambda" --policy-name voice-connections --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\", \"Action\": \"execute-api:ManageConnections\",
    \"Resource\": \"arn:aws:execute-api:$AWS_REGION:$ACCOUNT_ID:$API_ID/$STAGE/POST/@connections/*\"}]}"
echo "iam: $APP-lambda may reply on voice connections"

aws ssm get-parameter --name "$SSM/TWILIO_AUTH_TOKEN" >/dev/null 2>&1 \
  && echo "ssm: TWILIO_AUTH_TOKEN present" \
  || echo "next: add TWILIO_AUTH_TOKEN to SSM (see the header), then scripts/lambda_service.sh"
echo "Twilio webhook: POST $FN_URL/voice/incoming"
