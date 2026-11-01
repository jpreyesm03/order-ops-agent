#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the API. Safe to re-run: every step checks before creating.
#
#   ECR repository ──▶ images pushed by GitHub Actions (OIDC role, no stored AWS keys in GitHub)
#   Secrets Manager ──▶ DATABASE_URL, ADMIN_TOKEN, ANTHROPIC_API_KEY (injected into the ECS task at start)
#   IAM roles ──▶ ECS pulls the image and reads only these secrets; ECS Express manages the load balancer
#
# The ECS service itself is created by scripts/ecs_service.sh once the first image exists.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
exists() { "$@" >/dev/null 2>&1; }

# ---------- ECR ----------
if ! exists aws ecr describe-repositories --repository-names "$ECR_REPO"; then
  aws ecr create-repository --repository-name "$ECR_REPO" --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "ecr: created $ECR_REPO"
fi
aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" --lifecycle-policy-text '{"rules":[{"rulePriority":1,
  "description":"keep last 10 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},
  "action":{"type":"expire"}}]}' >/dev/null
echo "ecr: $ECR_URI (scan on push, keep last 10)"

# ---------- Secrets ----------
put_secret() {  # name, value-file
  local name="$1" file="$2"
  if exists aws secretsmanager describe-secret --secret-id "$name"; then
    echo "secret: $name exists (value unchanged)"
  else
    aws secretsmanager create-secret --name "$name" --secret-string "file://$file" >/dev/null
    echo "secret: created $name"
  fi
}
neon="$(grep -E '^NEON_DATABASE_URL=' .env | head -1 | cut -d= -f2-)"
[[ -n "$neon" ]] || { echo "NEON_DATABASE_URL missing in .env" >&2; exit 1; }
printf '%s' "$neon" > "$TMP/db"
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" > "$TMP/admin"
printf 'not-set-yet' > "$TMP/anthropic"
put_secret "$APP/database-url" "$TMP/db"
put_secret "$APP/admin-token" "$TMP/admin"
put_secret "$APP/anthropic-api-key" "$TMP/anthropic"
# Shopify app credentials (JSON: ECS injects each key as its own env var). Re-run updates the value from .env.
python3 - "$TMP/shopify" <<'PY'
import json, pathlib, sys
env = dict(l.split("=", 1) for l in pathlib.Path(".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
pathlib.Path(sys.argv[1]).write_text(json.dumps({k: env[k] for k in ("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")}))
PY
if exists aws secretsmanager describe-secret --secret-id "$APP/shopify"; then
  aws secretsmanager put-secret-value --secret-id "$APP/shopify" --secret-string "file://$TMP/shopify" >/dev/null
  echo "secret: $APP/shopify updated"
else
  put_secret "$APP/shopify" "$TMP/shopify"
fi
# Every secret of this app (prefix match), including ones added later. Listing ARNs instead raced with a
# just-created secret and left it out of the policy.
SECRET_ARNS="[\"arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT_ID:secret:$APP/*\"]"

role() {  # name, service principal
  if ! exists aws iam get-role --role-name "$1"; then
    aws iam create-role --role-name "$1" --assume-role-policy-document "{\"Version\":\"2012-10-17\",
      \"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
    echo "iam: created $1"
  fi
}

# ---------- IAM: ECS task execution (pull image, write logs, read only this app's secrets) ----------
role "$APP-ecs-execution" ecs-tasks.amazonaws.com
aws iam attach-role-policy --role-name "$APP-ecs-execution" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name "$APP-ecs-execution" --policy-name read-own-secrets --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",
    \"Resource\":$SECRET_ARNS}]}"

# ---------- IAM: ECS Express infrastructure (load balancer, security groups, scaling) ----------
role "$APP-ecs-infrastructure" ecs.amazonaws.com
aws iam attach-role-policy --role-name "$APP-ecs-infrastructure" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null 2>&1 || true

# ---------- IAM: GitHub Actions (OIDC) may push images, only from main of this repo ----------
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
    {\"Effect\":\"Allow\",\"Action\":[\"ecs:UpdateService\",\"ecs:DescribeServices\"],
     \"Resource\":\"arn:aws:ecs:$AWS_REGION:$ACCOUNT_ID:service/$APP/$APP-api\"}]}"

echo "done. GitHub deploy role: arn:aws:iam::$ACCOUNT_ID:role/$APP-github-deploy"
