#!/usr/bin/env bash
# Twilio phone number for the voice channel (D26), via the REST API.
# Needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in the environment.
#   scripts/twilio_number.sh list               5 available US local numbers with voice
#   scripts/twilio_number.sh buy +1XXXXXXXXXX   buy it and point "a call comes in" at the Lambda
#   scripts/twilio_number.sh show               numbers on the account and their webhooks
#   scripts/twilio_number.sh connect +1...      point an existing number (e.g. trial number) at the Lambda
#   scripts/twilio_number.sh verify +1...       trial only: verify your own phone (before buy)
set -euo pipefail
: "${TWILIO_ACCOUNT_SID:?export TWILIO_ACCOUNT_SID first}"
: "${TWILIO_AUTH_TOKEN:?export TWILIO_AUTH_TOKEN first}"

API="https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID"
WEBHOOK="https://woepit4n6ywq4satuxhu35lapi0yrnmk.lambda-url.us-west-2.on.aws/voice/incoming"
auth=(-u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN")

case "${1:-}" in
  list)
    curl -s "${auth[@]}" "$API/AvailablePhoneNumbers/US/Local.json?VoiceEnabled=true&PageSize=5" \
      | python3 -c 'import sys,json
d=json.load(sys.stdin)
if "available_phone_numbers" not in d: sys.exit(d.get("message", d))
for n in d["available_phone_numbers"]: print(n["phone_number"], n["locality"], n["region"])'
    ;;
  buy)
    : "${2:?usage: buy +1XXXXXXXXXX}"
    curl -s "${auth[@]}" -X POST "$API/IncomingPhoneNumbers.json" \
      --data-urlencode "PhoneNumber=$2" --data-urlencode "VoiceUrl=$WEBHOOK" --data-urlencode "VoiceMethod=POST" \
      | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("bought:", d["phone_number"], "->", d["voice_url"]) if "phone_number" in d else sys.exit(d.get("message", d))'
    ;;
  verify)  # trial accounts: verify your own phone first (Twilio calls it, you type the code shown here)
    : "${2:?usage: verify +1XXXXXXXXXX (your own phone)}"
    curl -s "${auth[@]}" -X POST "$API/OutgoingCallerIds.json" --data-urlencode "PhoneNumber=$2" \
      | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("Twilio is calling", d["phone_number"], "now. Answer and type this code on the keypad:", d["validation_code"]) if "validation_code" in d else sys.exit(d.get("message", d))'
    ;;
  connect)  # point a number you already have (e.g. the free trial number) at the Lambda
    : "${2:?usage: connect +1XXXXXXXXXX}"
    sid="$(curl -s "${auth[@]}" -G "$API/IncomingPhoneNumbers.json" --data-urlencode "PhoneNumber=$2" \
      | python3 -c 'import sys,json; n=json.load(sys.stdin).get("incoming_phone_numbers") or []; print(n[0]["sid"] if n else "")')"
    [[ -n "$sid" ]] || { echo "number $2 not on this account (run: $0 show)"; exit 1; }
    curl -s "${auth[@]}" -X POST "$API/IncomingPhoneNumbers/$sid.json" \
      --data-urlencode "VoiceUrl=$WEBHOOK" --data-urlencode "VoiceMethod=POST" \
      | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("connected:", d["phone_number"], "->", d["voice_url"]) if "phone_number" in d else sys.exit(d.get("message", d))'
    ;;
  show)
    curl -s "${auth[@]}" "$API/IncomingPhoneNumbers.json" \
      | python3 -c 'import sys,json
for n in json.load(sys.stdin)["incoming_phone_numbers"]: print(n["phone_number"], n["voice_method"], n["voice_url"])'
    curl -s "${auth[@]}" "$API.json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("account:", d.get("type"), d.get("status"))'
    curl -s "${auth[@]}" "$API/OutgoingCallerIds.json" \
      | python3 -c 'import sys,json; print("verified caller ids:", [n["phone_number"] for n in json.load(sys.stdin)["outgoing_caller_ids"]])'
    ;;
  *) echo "usage: $0 verify +1... | list | buy +1... | connect +1... | show"; exit 1 ;;
esac
