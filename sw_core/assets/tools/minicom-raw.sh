#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${MINICOM_RAW_DEVICE:-}" ]]; then
  echo "minicom-raw: MINICOM_RAW_DEVICE not set" >&2
  exit 2
fi
exec /usr/bin/minicom -D "${MINICOM_RAW_DEVICE}" "$@"
