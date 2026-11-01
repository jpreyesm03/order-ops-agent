#!/usr/bin/env bash
# Sourced by every AWS script. Pins the AWS CLI to the project's own account and stops if it resolves to any other.
#   source scripts/aws_env.sh
# Credentials: ~/.aws-personal (outside the repo). Override with AWS_ENV_DIR.

AWS_ENV_DIR="${AWS_ENV_DIR:-$HOME/.aws-personal}"
EXPECTED_ACCOUNT="054896499172"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE
export AWS_CONFIG_FILE="$AWS_ENV_DIR/config"
export AWS_SHARED_CREDENTIALS_FILE="$AWS_ENV_DIR/credentials"
export AWS_REGION="us-west-2" AWS_DEFAULT_REGION="us-west-2" AWS_PAGER=""

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
if [[ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]]; then
  echo "aws_env: credentials resolve to '${ACCOUNT_ID:-nothing}', expected $EXPECTED_ACCOUNT. Stopping." >&2
  return 1 2>/dev/null || exit 1
fi
export ACCOUNT_ID

# Names shared by the scripts
export APP=order-ops
export ECR_REPO=order-ops-api
export ECR_URI="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO"
export GITHUB_REPO=jpreyesm03/order-ops-agent
echo "aws_env: account $ACCOUNT_ID, region $AWS_REGION"
