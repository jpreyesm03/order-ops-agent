#!/usr/bin/env bash
# Creates the API's ECS Express service (Fargate task + HTTPS load balancer), or updates it to match this file.
# This file is the source of truth for the API's runtime config.
#   scripts/ecs_service.sh                                  # create / update with the image tagged :latest
#   IMAGE_TAG=<git sha> scripts/ecs_service.sh              # deploy a specific build
#   CORS_ORIGINS=https://x.vercel.app scripts/ecs_service.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/aws_env.sh

CLUSTER="$APP"
SERVICE="$APP-api"
LOG_GROUP="/ecs/$SERVICE"
CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:3005}"
IMAGE="$ECR_URI:${IMAGE_TAG:-latest}"

aws ecs describe-clusters --clusters "$CLUSTER" --query 'clusters[?status==`ACTIVE`].clusterName' --output text \
  | grep -q . || aws ecs create-cluster --cluster-name "$CLUSTER" >/dev/null
aws logs create-log-group --log-group-name "$LOG_GROUP" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 14

secret() { aws secretsmanager describe-secret --secret-id "$APP/$1" --query ARN --output text; }
CONTAINER="$(python3 - "$IMAGE" "$LOG_GROUP" "$CORS_ORIGINS" "$(secret database-url)" "$(secret admin-token)" \
  "$(secret anthropic-api-key)" <<'EOF'
import json, sys
image, logs, cors, db, admin, anthropic = sys.argv[1:]
env = {"CORS_ORIGINS": cors, "APPROVAL_THRESHOLD_USD": "250", "AGENT_MODEL": "claude-haiku-5-5",
       "AGENT_EFFORT": "medium", "MAX_TURNS_PER_SESSION": "40", "TURNS_PER_IP_PER_HOUR": "60", "TURNS_PER_DAY": "1500"}
print(json.dumps({
    "image": image, "containerPort": 8080,
    "awsLogsConfiguration": {"logGroup": logs, "logStreamPrefix": "api"},
    "environment": [{"name": k, "value": v} for k, v in env.items()],
    "secrets": [{"name": "DATABASE_URL", "valueFrom": db}, {"name": "ADMIN_TOKEN", "valueFrom": admin},
                {"name": "ANTHROPIC_API_KEY", "valueFrom": anthropic}],
}))
EOF
)"
# One task: rate limits live in process memory, and it caps spend.
SCALING='{"minTaskCount":1,"maxTaskCount":1}'
EXEC_ROLE="arn:aws:iam::$ACCOUNT_ID:role/$APP-ecs-execution"
SERVICE_ARN="arn:aws:ecs:$AWS_REGION:$ACCOUNT_ID:service/$CLUSTER/$SERVICE"

if aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" >/dev/null 2>&1; then
  aws ecs update-express-gateway-service --service-arn "$SERVICE_ARN" --execution-role-arn "$EXEC_ROLE" \
    --health-check-path /healthz --primary-container "$CONTAINER" --cpu 256 --memory 512 \
    --scaling-target "$SCALING" >/dev/null
  echo "ecs: updating $SERVICE ($IMAGE)"
else
  aws ecs create-express-gateway-service --cluster "$CLUSTER" --service-name "$SERVICE" \
    --execution-role-arn "$EXEC_ROLE" --infrastructure-role-arn "arn:aws:iam::$ACCOUNT_ID:role/$APP-ecs-infrastructure" \
    --health-check-path /healthz --primary-container "$CONTAINER" --cpu 256 --memory 512 \
    --scaling-target "$SCALING" >/dev/null
  echo "ecs: creating $SERVICE ($IMAGE)"
fi
echo "waiting for the service to become stable..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" --output json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d, default=str)[:0] or '', end='');
s=d.get('service', d); import re; urls=set(re.findall(r'https?://[a-z0-9.-]+\.(?:on\.aws|amazonaws\.com)[^\"]*', json.dumps(s)));
print('ecs: endpoint', ' '.join(sorted(urls)) or '(see console)')"
