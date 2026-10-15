#!/usr/bin/env bash
# Дополнительный discourse-cursor-bridge только для IMAGE_GEN (отдельный порт/workspace).
# Основной :8787 может оставаться для /chat (VLLM_FALLBACK); image-only инстансы снижают конкуренцию.
#
# Usage:
#   BRIDGE_ROOT=~/hoee/bridge/discourse-cursor-bridge ./scripts/setup-image-bridge-instance.sh 8789
#   systemctl --user enable --now discourse-cursor-bridge-image-8789.service
#
set -euo pipefail

PORT="${1:?usage: $0 <BRIDGE_PORT>}"
BRIDGE_ROOT="${BRIDGE_ROOT:-$HOME/hoee/bridge/discourse-cursor-bridge}"
BASE_ENV="${BRIDGE_ROOT}/.env"
INSTANCE_ENV="${BRIDGE_ROOT}/.env.image-${PORT}"
UNIT_NAME="discourse-cursor-bridge-image-${PORT}.service"
UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${UNIT_NAME}"

if [[ ! -f "$BASE_ENV" ]]; then
  echo "missing $BASE_ENV" >&2
  exit 1
fi

grep -v '^BRIDGE_PORT=' "$BASE_ENV" | grep -v '^CURSOR_OPENAI_WORKSPACE=' \
  | grep -v '^STATE_FILE=' | grep -v '^LOG_DIR=' >"$INSTANCE_ENV"
{
  echo "BRIDGE_PORT=${PORT}"
  echo "CURSOR_OPENAI_WORKSPACE=/tmp/cursor-openai-sandbox-${PORT}"
  echo "STATE_FILE=${BRIDGE_ROOT}/state-image-${PORT}.json"
  echo "LOG_DIR=${BRIDGE_ROOT}/logs-image-${PORT}"
} >>"$INSTANCE_ENV"

mkdir -p "/tmp/cursor-openai-sandbox-${PORT}" "${BRIDGE_ROOT}/logs-image-${PORT}"

cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Cursor bridge (image pool) :${PORT}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${BRIDGE_ROOT}
EnvironmentFile=${INSTANCE_ENV}
ExecStart=${BRIDGE_ROOT}/.venv/bin/python -u server.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "Wrote ${INSTANCE_ENV} and ${UNIT_PATH}"
echo "Start: systemctl --user enable --now ${UNIT_NAME}"
