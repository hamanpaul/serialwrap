#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "${SCRIPT_DIR}/minicom_router.sh" ]]; then
  exec "${SCRIPT_DIR}/minicom_router.sh" "$@"
fi
if [[ -x "${SCRIPT_DIR}/../tools/minicom_router.sh" ]]; then
  exec "${SCRIPT_DIR}/../tools/minicom_router.sh" "$@"
fi
if [[ -x "${SCRIPT_DIR}/../minicom_router.sh" ]]; then
  exec "${SCRIPT_DIR}/../minicom_router.sh" "$@"
fi

echo "minicom-broker: minicom_router.sh not found near ${SCRIPT_DIR}" >&2
exit 2
