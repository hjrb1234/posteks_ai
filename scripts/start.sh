#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .venv/bin/python ] || [ ! -f frontend/dist/index.html ]; then
  echo 'Сначала выполните bash scripts/install.sh'; exit 1
fi
.venv/bin/python -m scripts.manage init-db
set +e
.venv/bin/python -m scripts.manage has-admin
admin_status=$?
set -e
if [ "$admin_status" -eq 1 ]; then
  .venv/bin/python -m scripts.manage create-admin
elif [ "$admin_status" -ne 0 ]; then
  echo 'Не удалось проверить наличие администратора'; exit "$admin_status"
fi
printf '%s\n' 'Клиентский сайт: http://127.0.0.1:8080' 'Админка: http://127.0.0.1:8080/admin/knowledge' 'Остановка: Ctrl+C'
exec .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8080
