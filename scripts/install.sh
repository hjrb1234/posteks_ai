#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Нужен Python 3.11+"'
python3 -m scripts.check_server_stopped
command -v node >/dev/null || { echo 'Нужен Node.js 22 LTS или новее'; exit 1; }
node -e 'if(Number(process.versions.node.split(".")[0])<22)process.exit(1)'
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/storage/.cache/uv}"
if command -v uv >/dev/null; then
  if [ ! -f .venv/bin/python ]; then uv venv .venv; fi
  uv pip sync --require-hashes --python .venv/bin/python requirements.lock
else
  if [ ! -f .venv/bin/python ]; then python3 -m venv .venv; fi
  .venv/bin/python -m pip install --require-hashes -r requirements.lock
fi
if [ ! -f .env ]; then cp .env.example .env; fi
if command -v tesseract >/dev/null; then
  .venv/bin/python -m scripts.install_ocr
else
  echo 'Tesseract не установлен: PDF с текстом доступны, сканы потребуют установки Tesseract 5.'
fi
.venv/bin/python -m scripts.manage prepare-update
.venv/bin/python -m scripts.manage init-db
.venv/bin/python -m scripts.manage import-sources
(cd frontend && npm ci --cache ../storage/.cache/npm && npm run build)
echo 'Установка завершена. Создайте администратора: .venv/bin/python -m scripts.manage create-admin'
