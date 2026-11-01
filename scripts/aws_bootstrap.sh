#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the API. Safe to re-run: every step checks before creating.
#
#   ECR repository ──▶ images pushed by GitHub Actions (OIDC role, no stored AWS keys in GitHub)
#   Secrets Manager ──▶ DATABASE_URL, ADMIN_TOKEN, ANTHROPIC_API_KEY (read by App Runner at start)
#   IAM roles ──▶ App Runner pulls from ECR; the running service reads only its own secrets
#
# The App Runner service itself is created by scripts/apprunner_service.sh once the first image exists.
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
SECRET_ARNS="$(aws secretsmanager list-secrets --filters Key=name,Values="$APP/" \
  --query 'SecretList[].ARN' --output json)"

# ---------- IAM: App Runner pulls images from ECR ----------
if ! exists aws iam get-role --role-name "$APP-apprunner-ecr"; then
  aws iam create-role --role-name "$APP-apprunner-ecr" --assume-role-policy-document '{"Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  aws iam attach-role-policy --role-name "$APP-apprunner-ecr" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  echo "iam: created $APP-apprunner-ecr"
fi

# ---------- IAM: the running service may read its own secrets, nothing else ----------
if ! exists aws iam get-role --role-name "$APP-apprunner-instance"; then
  aws iam create-role --role-name "$APP-apprunner-instance" --assume-role-policy-document '{"Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  echo "iam: created $APP-apprunner-instance"
fi
aws iam put-role-policy --role-name "$APP-apprunner-instance" --policy-name read-own-secrets --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",
    \"Resource\":$SECRET_ARNS}]}"

# ---------- IAM: GitHub Actions (OIDC) may push images, only from main of this repo ----------
OIDC_ARN="arn:aws:iam::$ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
if ! exists aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN"; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
  echo "iam: created GitHub OIDC provider"
fi
if ! exists aws iam get-role --role-name "$APP-github-deploy"; then
  aws iam create-role --role-name "$APP-github-deploy" --assume-role-policy-document "{\"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":\"$OIDC_ARN\"},
    \"Action\":\"sts:AssumeRoleWithWebIdentity\",\"Condition\":{
      \"StringEquals\":{\"token.actions.githubusercontent.com:aud\":\"sts.amazonaws.com\",
                        \"token.actions.githubusercontent.com:sub\":\"repo:$GITHUB_REPO:ref:refs/heads/main\"}}}]}" \
    >/dev/null
  echo "iam: created $APP-github-deploy"
fi
aws iam put-role-policy --role-name "$APP-github-deploy" --policy-name push-api-image --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:BatchGetImage\",
      \"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",\"ecr:CompleteLayerUpload\",\"ecr:PutImage\"],
     \"Resource\":\"arn:aws:ecr:$AWS_REGION:$ACCOUNT_ID:repository/$ECR_REPO\"}]}"

echo "done. GitHub deploy role: arn:aws:iam::$ACCOUNT_ID:role/$APP-github-deploy"
