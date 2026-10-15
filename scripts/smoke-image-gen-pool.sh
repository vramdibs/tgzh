#!/usr/bin/env bash
# Smoke: пул IMAGE_GEN bridge + отсутствие 429 при очереди (юниты + живые порты).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORTS=(8787 8789)
if [[ -n "${IMAGE_GEN_SMOKE_PORTS:-}" ]]; then
  IFS=',' read -ra PORTS <<<"${IMAGE_GEN_SMOKE_PORTS}"
fi

KEY="${IMAGE_GEN_API_KEY:-${BRIDGE_OPENAI_API_KEY:-}}"
if [[ -z "$KEY" && -f .env ]]; then
  KEY="$(grep -E '^IMAGE_GEN_API_KEY=' .env | head -1 | cut -d= -f2- || true)"
fi

echo "== bridge /v1/models =="
for p in "${PORTS[@]}"; do
  code="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${KEY:-x}" "http://127.0.0.1:${p}/v1/models" || echo 000)"
  if [[ "$code" != "200" && "$code" != "401" ]]; then
    echo "port ${p}: unexpected HTTP ${code}" >&2
    exit 1
  fi
  echo "port ${p}: ok (${code})"
done

echo "== pytest pool (FIFO, no 429 contract) =="
pytest -q tests/test_image_gen.py -k bridge_pool
pytest -q tests/test_server.py::test_image_generate_three_parallel_no_429

if [[ -n "${SERVER_URL:-}" ]]; then
  echo "== tgzh-server max concurrent =="
  docker compose exec -T tgzh-server python -c \
    'import image_gen; print("urls", image_gen.image_gen_base_urls()); print("max", image_gen.image_gen_max_concurrent())'
fi

echo "smoke-image-gen-pool: ok"
