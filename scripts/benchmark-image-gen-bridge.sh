#!/usr/bin/env bash
# A/B время генерации на локальном cursor-bridge (без tgzh-server).
# Использование из корня репозитория:
#   set -a && source .env && set +a
#   ./scripts/benchmark-image-gen-bridge.sh
# Нужны IMAGE_GEN_API_KEY; URL по умолчанию http://127.0.0.1:8787/v1

set -euo pipefail

BASE_URL="${IMAGE_GEN_BENCH_BASE_URL:-http://127.0.0.1:8787/v1}"
KEY="${IMAGE_GEN_API_KEY:-}"
MODEL="${IMAGE_GEN_MODEL:-auto}"
PROMPT="${IMAGE_GEN_BENCH_PROMPT:-solid red circle on white background, minimal}"

if [[ -z "$KEY" ]]; then
  echo "IMAGE_GEN_API_KEY is empty" >&2
  exit 1
fi

run_one() {
  local size="$1"
  local t0 t1 elapsed http_code
  t0=$(date +%s.%N)
  http_code=$(curl -sS -o /tmp/tgzh-bench-img.out -w '%{http_code}' \
    -m "${IMAGE_GEN_BENCH_CURL_MAX_SEC:-600}" \
    -X POST "${BASE_URL%/}/images/generations" \
    -H "Authorization: Bearer ${KEY}" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${MODEL}\",\"prompt\":\"${PROMPT}\",\"size\":\"${size}\",\"n\":1}")
  t1=$(date +%s.%N)
  elapsed=$(python3 -c "print(round(float('${t1}') - float('${t0}'), 2))")
  local bytes=0
  if [[ -f /tmp/tgzh-bench-img.out ]]; then
    bytes=$(wc -c < /tmp/tgzh-bench-img.out)
  fi
  echo "size=${size} http=${http_code} elapsed_s=${elapsed} response_bytes=${bytes}"
  if [[ "$http_code" != "200" ]]; then
    return 1
  fi
  return 0
}

echo "bridge=${BASE_URL} model=${MODEL}"
fail=0
run_one "512x512" || fail=1
run_one "1024x1024" || fail=1
exit "$fail"
