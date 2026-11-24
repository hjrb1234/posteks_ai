#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .venv/bin/python ] || [ ! -f frontend/dist/index.html ]; then
  echo 'Сначала выполните bash scripts/install.sh'; exit 1
fi
if [ "${CODESPACES:-}" = 'true' ] && [ -n "${CODESPACE_NAME:-}" ]; then
  export PUBLIC_BASE_URL="https://${CODESPACE_NAME}-8080.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  export ALLOWED_ORIGINS="$PUBLIC_BASE_URL"
  export COOKIE_SECURE=true
fi
.venv/bin/python -m scripts.manage init-db
if curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
  echo 'Сервис уже запущен. Откройте private-порт8080 во вкладке Ports.'; exit 0
fi
mkdir -p storage
nohup .venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port 8080 > storage/codespaces.log 2>&1 &
printf '%s\n' "$!" > storage/codespaces.pid
for attempt in {1..40}; do
  if curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo 'Сервис готов. Откройте порт8080 в Ports, сохранив видимость Private.'
    echo 'Первый администратор: .venv/bin/python -m scripts.manage create-admin'
    exit 0
  fi
  sleep .25
done
echo 'Не удалось дождаться сервиса. Проверьте storage/codespaces.log'; exit 1
