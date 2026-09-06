#!/usr/bin/env bash
# Smoke: motok hub + tgzh-server + LM Studio (текстовая проверка).
# Использование (из корня tgzh):
#   set -a && source ../motok/local/hub.env && set +a
#   export MOTOK_HUB_URL=http://127.0.0.1:54326   # с хоста, не host.docker.internal
#   bash scripts/smoke_homework_stack.sh
set -euo pipefail

HUB_URL="${MOTOK_HUB_URL:-http://127.0.0.1:54326}"
HUB_URL="${HUB_URL%/}"
SERVER_URL="${SERVER_URL:-http://127.0.0.1:8000}"
SERVER_URL="${SERVER_URL%/}"
LMS_URL="${VLLM_BASE_URL:-http://127.0.0.1:1234/v1}"
LMS_URL="${LMS_URL%/chat/completions}"
LMS_URL="${LMS_URL%/}"
LMS_URL="${LMS_URL%/v1}/v1"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }

echo "== hub health =="
hub_json="$(curl -fsS -m 10 "${HUB_URL}/health")" || fail "hub ${HUB_URL}/health"
echo "$hub_json"
echo "$hub_json" | grep -q '"module":"homework"' || fail "hub module"

echo "== LM Studio models =="
models_json="$(curl -fsS -m 15 "${LMS_URL}/models")" || fail "LM Studio ${LMS_URL}/models"
echo "$models_json" | head -c 400
echo

echo "== tgzh-server health =="
srv_json="$(curl -fsS -m 10 "${SERVER_URL}/health")" || fail "server ${SERVER_URL}/health"
echo "$srv_json"

if [[ -z "${MOTOK_HUB_TOKEN_SECRET:-}" ]]; then
  fail "MOTOK_HUB_TOKEN_SECRET not set"
fi
if [[ -z "${MOTOK_INTERNAL_TOKEN:-}" ]]; then
  fail "MOTOK_INTERNAL_TOKEN not set"
fi

echo "== hub upsert telegram (smoke user) =="
upsert_body="$(curl -fsS -m 15 -X POST "${HUB_URL}/internal/telegram/upsert" \
  -H "x-motok-internal: ${MOTOK_INTERNAL_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{"telegram_user_id":"smoke-test-1"}')"
echo "$upsert_body"
token="$(python3 -c "import json,sys; print(json.load(sys.stdin)['token'])" <<<"$upsert_body")"
[[ -n "$token" ]] || fail "empty jwt from hub"

echo "== POST /check (text) =="
tmp="$(mktemp)"
printf '%s' '2+2=4' >"$tmp"
check_out="$(curl -fsS -m 300 -X POST "${SERVER_URL}/check" \
  -H "Authorization: Bearer ${token}" \
  -F 'grade=6' \
  -F 'textbook_label=Smoke' \
  -F 'paragraph=1' \
  -F "photo=@${tmp};type=text/plain")"
rm -f "$tmp"
echo "$check_out" | head -c 500
echo
echo "$check_out" | grep -q '"result"' || fail "/check response"
if echo "$check_out" | grep -qi 'не удалось подключиться к vllm'; then
  fail "VLLM unreachable from tgzh-server (LM Studio? docker-compose.lmstudio.yml?)"
fi
if echo "$check_out" | grep -qi 'ошибка vllm'; then
  echo "WARN: VLLM returned error (model loaded but check failed) — wiring OK"
fi
ok "homework stack smoke passed"
