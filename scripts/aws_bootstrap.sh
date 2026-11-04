#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the API. Safe to re-run: every step checks before creating.
#
#   ECR repository ──▶ images pushed by GitHub Actions (OIDC role, no stored AWS keys in GitHub), pulled by Lambda
#   SSM Parameter Store ──▶ DATABASE_URL, ADMIN_TOKEN, ANTHROPIC_API_KEY, Shopify app (SecureString, free tier),
#                           read by the function at cold start
#   IAM roles ──▶ the function reads only these parameters; the nightly schedule may only invoke the function;
#                 GitHub may only push the image and update the function's code
#
# The function itself is created by scripts/lambda_service.sh once the first image exists.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
exists() { "$@" >/dev/null 2>&1; }
FUNCTION_ARN="arn:aws:lambda:$AWS_REGION:$ACCOUNT_ID:function:$APP-api"

# ---------- ECR ----------
if ! exists aws ecr describe-repositories --repository-names "$ECR_REPO"; then
  aws ecr create-repository --repository-name "$ECR_REPO" --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "ecr: created $ECR_REPO"
fi
aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" --lifecycle-policy-text '{"rules":[{"rulePriority":1,
  "description":"keep last 10 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},
  "action":{"type":"expire"}}]}' >/dev/null
aws ecr set-repository-policy --repository-name "$ECR_REPO" --policy-text "{\"Version\":\"2008-10-17\",\"Statement\":[{
  \"Sid\":\"LambdaPull\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},
  \"Action\":[\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],
  \"Condition\":{\"StringLike\":{\"aws:sourceArn\":\"$FUNCTION_ARN\"}}}]}" >/dev/null
echo "ecr: $ECR_URI (scan on push, keep last 10, pullable by $APP-api)"

# ---------- Parameters (SecureString) ----------
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
put_param() {  # name, value-file, overwrite(true|false)
  local name="/$APP/$1" file="$2" overwrite="$3"
  if exists aws ssm get-parameter --name "$name" && [[ "$overwrite" != true ]]; then
    echo "param: $name exists (value unchanged)"
  else
    aws ssm put-parameter --name "$name" --type SecureString --overwrite --value "file://$file" >/dev/null
    echo "param: $name set"
  fi
}
neon="$(env_value NEON_DATABASE_URL)"
[[ -n "$neon" ]] || { echo "NEON_DATABASE_URL missing in .env" >&2; exit 1; }
printf '%s' "$neon" > "$TMP/db"
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" > "$TMP/admin"
key="$(env_value ANTHROPIC_API_KEY)"; printf '%s' "${key:-not-set-yet}" > "$TMP/anthropic"
printf '%s' "$(env_value SHOPIFY_CLIENT_ID)" > "$TMP/shopify_id"
printf '%s' "$(env_value SHOPIFY_CLIENT_SECRET)" > "$TMP/shopify_secret"
put_param DATABASE_URL "$TMP/db" false
put_param ADMIN_TOKEN "$TMP/admin" false
put_param ANTHROPIC_API_KEY "$TMP/anthropic" false
put_param SHOPIFY_CLIENT_ID "$TMP/shopify_id" true       # re-run syncs the Shopify app from .env
put_param SHOPIFY_CLIENT_SECRET "$TMP/shopify_secret" true

role() {  # name, service principal
  if ! exists aws iam get-role --role-name "$1"; then
    aws iam create-role --role-name "$1" --assume-role-policy-document "{\"Version\":\"2012-10-17\",
      \"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
    echo "iam: created $1"
  fi
}

# ---------- IAM: the function (logs, read only this app's parameters, invoke itself for background work) ----------
role "$APP-lambda" lambda.amazonaws.com
aws iam attach-role-policy --role-name "$APP-lambda" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name "$APP-lambda" --policy-name params-and-self-invoke --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParametersByPath\",
     \"Resource\":[\"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$APP\",
                 \"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$APP/*\"]},
    {\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$FUNCTION_ARN\"}]}"

# ---------- IAM: EventBridge Scheduler (nightly reseed) may invoke the function, nothing else ----------
role "$APP-scheduler" scheduler.amazonaws.com
aws iam put-role-policy --role-name "$APP-scheduler" --policy-name invoke-api --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",
    \"Resource\":\"$FUNCTION_ARN\"}]}"

# ---------- IAM: GitHub Actions (OIDC) may push images and update the function, only from main of this repo ----------
OIDC_ARN="arn:aws:iam::$ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
if ! exists aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN"; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
  echo "iam: created GitHub OIDC provider"
fi
# GitHub's subject prefix (immutable form "repo:owner@id/name@id" when enabled for the repo)
SUB_PREFIX="$(gh api "repos/$GITHUB_REPO/actions/oidc/customization/sub" --jq '.sub_claim_prefix // empty' 2>/dev/null)"
SUB_PREFIX="${SUB_PREFIX:-repo:$GITHUB_REPO}"
TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":\"$OIDC_ARN\"},
  \"Action\":\"sts:AssumeRoleWithWebIdentity\",\"Condition\":{
    \"StringEquals\":{\"token.actions.githubusercontent.com:aud\":\"sts.amazonaws.com\",
                      \"token.actions.githubusercontent.com:sub\":\"$SUB_PREFIX:ref:refs/heads/main\"}}}]}"
if ! exists aws iam get-role --role-name "$APP-github-deploy"; then
  aws iam create-role --role-name "$APP-github-deploy" --assume-role-policy-document "$TRUST" >/dev/null
  echo "iam: created $APP-github-deploy"
fi
aws iam update-assume-role-policy --role-name "$APP-github-deploy" --policy-document "$TRUST"
echo "iam: $APP-github-deploy trusts $SUB_PREFIX:ref:refs/heads/main"
aws iam put-role-policy --role-name "$APP-github-deploy" --policy-name push-api-image --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:BatchGetImage\",
      \"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",\"ecr:CompleteLayerUpload\",\"ecr:PutImage\"],
     \"Resource\":\"arn:aws:ecr:$AWS_REGION:$ACCOUNT_ID:repository/$ECR_REPO\"},
    {\"Effect\":\"Allow\",\"Action\":[\"lambda:UpdateFunctionCode\",\"lambda:GetFunction\",
      \"lambda:GetFunctionConfiguration\"],\"Resource\":\"$FUNCTION_ARN\"}]}"

echo "done. GitHub deploy role: arn:aws:iam::$ACCOUNT_ID:role/$APP-github-deploy"
