# Voice channel: runbook (D26)

A phone call through Twilio ConversationRelay, answered by the same agent and policy gate as the chat.

## 1. Without a phone or keys (tests)
```bash
uv run pytest -q tests/test_voice.py    # fake Twilio over a real websocket, scripted model, real gate + Postgres
```

## 2. Voice eval (needs the Anthropic key, ~$1)
```bash
uv run python -m order_ops.evaluate --split test --trials 4 --channel voice --label own_voice
uv run python -m order_ops.report       # "Chat → voice" row: paired gap vs. own_single_on
```

## 3. A real call from your laptop (Twilio account + a number, ~$1.15/month)
```bash
# .env: TWILIO_AUTH_TOKEN=..., VOICE_RELAY_SECRET=<any long random string>
cloudflared tunnel --url http://localhost:8055          # prints https://<name>.trycloudflare.com
# .env: VOICE_PUBLIC_URL=https://<name>.trycloudflare.com  VOICE_WS_URL=wss://<name>.trycloudflare.com/voice/ws
uv run uvicorn order_ops.api:app --port 8055
```
Twilio console → the number → "A call comes in": Webhook, POST, `https://<name>.trycloudflare.com/voice/incoming`.

## 4. On AWS (the live demo)
```bash
scripts/voice_aws.sh       # API Gateway WebSocket → the Lambda; SSM params; lets the role reply on connections
aws ssm put-parameter --name /order-ops/TWILIO_AUTH_TOKEN --type SecureString --value <token>
scripts/lambda_service.sh  # new cold start picks up the parameters
```
Then point the number's webhook at `<Function URL>/voice/incoming`.

## What to check on a real call
- Time to first audio per turn: `state->'_extra'->'voice'->'turns'` in the sessions table (target p95 < 1.5 s).
- "yeah, I guess" before a change → the agent asks again (gate: `confirmation` blocked, transcript `hedged: true`).
- Interrupt mid-sentence → next reply doesn't assume you heard the rest.
- A refund over $250 → "submitted for supervisor review", visible in the console.
