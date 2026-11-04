#!/usr/bin/env bash
# Points the `live` alias back at an earlier version (takes effect in about a second; no rebuild).
#   scripts/lambda_rollback.sh              # list recent versions and where `live` points
#   scripts/lambda_rollback.sh <version>    # move `live` to that version, after checking it
# Versions run the image they were published with; ECR keeps the last 10 images, so that is the rollback window.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh
FUNCTION="$APP-api"

CURRENT="$(aws lambda get-alias --function-name "$FUNCTION" --name live --query FunctionVersion --output text)"
if [[ $# -eq 0 ]]; then
  echo "live -> $CURRENT"
  aws lambda list-versions-by-function --function-name "$FUNCTION" \
    --query 'Versions[?Version!=`$LATEST`].[Version,LastModified,Description]' --output text | tail -10
  exit 0
fi
scripts/lambda_smoke.sh "$1"
aws lambda update-alias --function-name "$FUNCTION" --name live --function-version "$1" >/dev/null
echo "lambda: live $CURRENT -> $1"
