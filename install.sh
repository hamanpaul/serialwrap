#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${HOME}/.paul_tools}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ${0} [install_dir]"
  echo "Default install_dir: ${HOME}/.paul_tools"
  exit 0
fi

mkdir -p "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/sw_core"
mkdir -p "${TARGET_DIR}/sw_mcp"
mkdir -p "${TARGET_DIR}/profiles"
mkdir -p "${TARGET_DIR}/tools"
mkdir -p "${TARGET_DIR}/docs"

install -m 0755 "${SCRIPT_DIR}/serialwrap" "${TARGET_DIR}/serialwrap"
install -m 0755 "${SCRIPT_DIR}/serialwrapd.py" "${TARGET_DIR}/serialwrapd.py"
install -m 0755 "${SCRIPT_DIR}/serialwrap-mcp" "${TARGET_DIR}/serialwrap-mcp"
install -m 0755 "${SCRIPT_DIR}/tools/minicom_router.sh" "${TARGET_DIR}/minicom_router.sh"
install -m 0755 "${SCRIPT_DIR}/tools/minicom-broker.sh" "${TARGET_DIR}/minicom-broker.sh"
install -m 0755 "${SCRIPT_DIR}/tools/minicom-raw.sh" "${TARGET_DIR}/minicom-raw.sh"

cp -a "${SCRIPT_DIR}/sw_core/." "${TARGET_DIR}/sw_core/"
cp -a "${SCRIPT_DIR}/sw_mcp/." "${TARGET_DIR}/sw_mcp/"
cp -a "${SCRIPT_DIR}/profiles/." "${TARGET_DIR}/profiles/"
cp -a "${SCRIPT_DIR}/tools/." "${TARGET_DIR}/tools/"
cp -a "${SCRIPT_DIR}/docs/." "${TARGET_DIR}/docs/"

DEFAULT_PROFILE_PATH="${TARGET_DIR}/profiles/default.yaml"
DEFAULT_PLACEHOLDER_BY_ID="/dev/serial/by-id/target0"
INSTALL_AUTOBIND="${SERIALWRAP_INSTALL_AUTOBIND:-1}"

if [[ "${INSTALL_AUTOBIND}" == "1" && -f "${DEFAULT_PROFILE_PATH}" ]]; then
  mapfile -t by_id_entries < <(compgen -G "/dev/serial/by-id/*" || true)
  if /usr/bin/grep -Fq "${DEFAULT_PLACEHOLDER_BY_ID}" "${DEFAULT_PROFILE_PATH}" \
    && [[ "${#by_id_entries[@]}" -eq 1 ]] \
    && [[ -e "${by_id_entries[0]}" ]]; then
    /usr/bin/sed -i "s#${DEFAULT_PLACEHOLDER_BY_ID}#${by_id_entries[0]}#g" "${DEFAULT_PROFILE_PATH}"
    echo "[serialwrap] auto-bind default target to: ${by_id_entries[0]}"
  fi
fi

cat <<MSG
[serialwrap] install done
  target: ${TARGET_DIR}
  binary: ${TARGET_DIR}/serialwrap
  daemon: ${TARGET_DIR}/serialwrapd.py
  minicom router: ${TARGET_DIR}/minicom_router.sh

Suggested shell setup:
  export PATH="${TARGET_DIR}:\$PATH"
  alias minicom="${TARGET_DIR}/minicom_router.sh"
MSG
