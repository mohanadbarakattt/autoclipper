#!/usr/bin/env bash
# Checks a deployed clips service from anywhere: HTTPS, health, token auth, user + channel round trip.
#   CLIPS_SERVICE_URL=https://clips-api.virlo.studio CLIPS_SERVICE_TOKEN=... bash deploy/smoke_test.sh
# Leaves nothing behind except one paused test user (no channels).
set -euo pipefail
URL="${CLIPS_SERVICE_URL:?set CLIPS_SERVICE_URL}"
TOK="${CLIPS_SERVICE_TOKEN:?set CLIPS_SERVICE_TOKEN}"
U="smoke-test-0000"
H=(-H "Authorization: Bearer $TOK" -H "Content-Type: application/json")
pass() { printf '  \033[32mok\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/health") ; [ "$code" = 200 ] && pass "health over HTTPS" || fail "health -> $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/v1/users/$U/overview") ; [ "$code" = 401 ] && pass "rejects calls without the token" || fail "no-token -> $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/signup") ; [ "$code" = 404 ] && pass "standalone pages are off" || fail "/signup -> $code"
curl -fsS "${H[@]}" -X PUT "$URL/v1/users/$U" -d '{"tier":"free"}' >/dev/null && pass "create test user"
ch=$(curl -fsS "${H[@]}" -X POST "$URL/v1/users/$U/channels" -d '{"link":"https://twitch.tv/smoketest_channel","style":"gaming","rights":"own"}')
id=$(printf '%s' "$ch" | python3 -c 'import sys,json; print(json.load(sys.stdin)["channel"]["id"])') && pass "add channel ($id)"
curl -fsS "${H[@]}" "$URL/v1/users/$U/overview" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["limits"]["name"]=="free"' && pass "overview"
curl -fsS "${H[@]}" -X DELETE "$URL/v1/users/$U/channels/$id" >/dev/null && pass "remove channel"
echo "All good."
