#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: ${0} [setup-args...]"
  echo "以 pipx 安裝 serialwrap（隔離 venv）並執行 serialwrap setup；額外參數轉給 setup。"
  exit 0
fi
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "${SCRIPT_DIR}"
else
  echo "[serialwrap] 未找到 pipx，改用 pip install --user（建議改用 pipx 取得隔離 venv）" >&2
  python3 -m pip install --user --force-reinstall "${SCRIPT_DIR}"
fi
hash -r 2>/dev/null || true
echo "[serialwrap] 套件已安裝；接著執行 serialwrap setup ..."
if command -v serialwrap >/dev/null 2>&1; then
  serialwrap setup "$@"
else
  cat >&2 <<'MSG'
[serialwrap] serialwrap 不在 PATH。請確認 ~/.local/bin 在 PATH（pipx 可用 `pipx ensurepath`），
重開 shell 後執行：serialwrap setup
MSG
  exit 1
fi
