#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${AGROCAST_PY:-../.venv/bin/python}"
"$PY" -m PyInstaller --noconfirm --distpath "${AGROCAST_DIST:-dist}" desktop/AgroCast.spec
echo "Готово: ${AGROCAST_DIST:-dist}/AgroCast (запуск: ${AGROCAST_DIST:-dist}/AgroCast/AgroCast)"
