#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for router_name in serialwrap-minicom minicom_router.sh; do
  for router_path in \
    "${SCRIPT_DIR}/${router_name}" \
    "${SCRIPT_DIR}/../tools/${router_name}" \
    "${SCRIPT_DIR}/../${router_name}"; do
    if [[ -x "${router_path}" ]]; then
      exec "${router_path}" "$@"
    fi
  done
done

echo "minicom-broker: serialwrap-minicom or minicom_router.sh not found near ${SCRIPT_DIR}" >&2
exit 2
