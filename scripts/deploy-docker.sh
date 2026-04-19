#!/usr/bin/env bash
# Синхронизирует проект на удалённый хост и пересобирает/перезапускает контейнеры
# Использование: DEPLOY_HOST=user@host ./scripts/deploy-docker.sh
# Опционально: DEPLOY_REMOTE_PATH (абсолютный путь на сервере; иначе ~/tgzh-docker)
# Опционально: DEPLOY_COMPOSE_PROFILE=preocr — то же, что docker compose --profile preocr (tgzh-preocr)
# С PostgreSQL: DEPLOY_RUN_ALEMBIC=1 - после up выполнить alembic upgrade head в tgzh-bot
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_HOST="${DEPLOY_HOST:?Set DEPLOY_HOST, e.g. user@example.com}"

if [[ -n "${DEPLOY_REMOTE_PATH:-}" ]]; then
  RD="$DEPLOY_REMOTE_PATH"
else
  RD=$(ssh "${DEPLOY_HOST}" 'printf %s "$HOME/tgzh-docker"')
fi

ssh "${DEPLOY_HOST}" "mkdir -p -- ${RD}"

(
  cd "${ROOT}"
  tar czf - \
    --exclude='./.git' \
    --exclude='./__pycache__' \
    --exclude='*.pyc' \
    --exclude='./.env' \
    --exclude='./data/users.sqlite' \
    --exclude='./data/gdz_cache' \
    --exclude='./venv' \
    --exclude='./.venv' \
    .
) | ssh "${DEPLOY_HOST}" "tar xzf - -C ${RD}"

if [[ -f "${ROOT}/.env" ]]; then
  scp "${ROOT}/.env" "${DEPLOY_HOST}:${RD}/.env"
fi

PF=""
if [[ -n "${DEPLOY_COMPOSE_PROFILE:-}" ]]; then
  PF=" --profile ${DEPLOY_COMPOSE_PROFILE}"
fi
ssh "${DEPLOY_HOST}" "cd ${RD} && docker compose${PF} build && docker compose${PF} up -d --force-recreate"

if [[ "${DEPLOY_RUN_ALEMBIC:-}" == "1" ]]; then
  ssh "${DEPLOY_HOST}" "cd ${RD} && docker compose exec -T tgzh-bot alembic upgrade head"
fi

echo "Done: ${DEPLOY_HOST}:${RD}"
