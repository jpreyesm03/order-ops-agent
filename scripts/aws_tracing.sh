#!/usr/bin/env bash
# One-time (idempotent) setup so the API's OpenTelemetry spans land in AWS X-Ray (D24):
#   1. CloudWatch Transaction Search: X-Ray stores spans in the `aws/spans` log group (needed for the OTLP endpoint);
#      1% of spans indexed as trace summaries (the free default).
#   2. The function's role may write spans (managed policy AWSXrayWriteOnlyAccess).
# Then turn tracing on in the function's config: TRACING=1 scripts/lambda_service.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

aws logs put-resource-policy --policy-name "$APP-xray-spans" --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"TransactionSearchXRayAccess\", \"Effect\": \"Allow\",
    \"Principal\": {\"Service\": \"xray.amazonaws.com\"}, \"Action\": \"logs:PutLogEvents\",
    \"Resource\": [\"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:aws/spans:*\",
                   \"arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:/aws/application-signals/data:*\"],
    \"Condition\": {\"ArnLike\": {\"aws:SourceArn\": \"arn:aws:xray:$AWS_REGION:$ACCOUNT_ID:*\"},
                    \"StringEquals\": {\"aws:SourceAccount\": \"$ACCOUNT_ID\"}}}]}" >/dev/null
echo "logs: resource policy for X-Ray spans"

if [[ "$(aws xray get-trace-segment-destination --query Destination --output text)" != "CloudWatchLogs" ]]; then
  aws xray update-trace-segment-destination --destination CloudWatchLogs >/dev/null
  echo "xray: Transaction Search enabled (spans searchable after ~10 min)"
fi

aws iam attach-role-policy --role-name "$APP-lambda" --policy-arn arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess
echo "iam: $APP-lambda may write spans"
aws logs put-retention-policy --log-group-name aws/spans --retention-in-days 14 2>/dev/null \
  && echo "logs: aws/spans kept 14 days" || echo "logs: aws/spans not created yet (first spans create it; re-run to set retention)"
