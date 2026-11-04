#!/usr/bin/env bash
# Calls GET /healthz on one specific version of the function, before any traffic reaches it.
# The event has the Function URL shape, so it goes through the Web Adapter and uvicorn like a real request.
#   scripts/lambda_smoke.sh <version|alias>        (CI calls it with the AWS credentials it already has)
set -euo pipefail
FUNCTION="${FUNCTION:-order-ops-api}"
QUALIFIER="$1"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 - > "$TMP/event.json" <<'EOF'
import json, time
print(json.dumps({
    "version": "2.0", "routeKey": "$default", "rawPath": "/healthz", "rawQueryString": "",
    "headers": {"host": "smoke.test", "user-agent": "lambda-smoke"}, "isBase64Encoded": False,
    "requestContext": {"accountId": "anonymous", "apiId": "smoke", "domainName": "smoke.test", "domainPrefix": "smoke",
                       "http": {"method": "GET", "path": "/healthz", "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1",
                                "userAgent": "lambda-smoke"},
                       "requestId": "smoke", "routeKey": "$default", "stage": "$default",
                       "time": time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime()),
                       "timeEpoch": int(time.time() * 1000)}}))
EOF
for attempt in 1 2 3; do  # the first call to a new version is a cold start
  if aws lambda invoke --function-name "$FUNCTION" --qualifier "$QUALIFIER" --cli-read-timeout 60 \
       --payload "fileb://$TMP/event.json" "$TMP/out.json" >/dev/null 2>"$TMP/err" \
     && python3 - "$TMP/out.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
body = json.loads(r.get("body") or "{}")
ok = r.get("statusCode") == 200 and body.get("ok") is True
print(f"smoke: status {r.get('statusCode')}, body {body}")
sys.exit(0 if ok else 1)
EOF
  then
    echo "smoke: $FUNCTION:$QUALIFIER healthy"; exit 0
  fi
  cat "$TMP/err" >&2 || true
  sleep 3
done
echo "smoke: $FUNCTION:$QUALIFIER failed /healthz" >&2
exit 1
