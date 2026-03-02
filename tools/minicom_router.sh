#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "${SCRIPT_DIR}/serialwrap" ]]; then
  BASE_DIR="${SCRIPT_DIR}"
elif [[ -x "${SCRIPT_DIR}/../serialwrap" ]]; then
  BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  BASE_DIR="${SCRIPT_DIR}"
fi

SERIALWRAP_BIN="${SERIALWRAP_BIN:-${BASE_DIR}/serialwrap}"
SOCKET="${SERIALWRAP_SOCKET:-/tmp/serialwrap/serialwrapd.sock}"
PROFILE_DIR="${SERIALWRAP_PROFILE_DIR:-${BASE_DIR}/profiles}"
AUTO_START_DAEMON="${SERIALWRAP_AUTO_START_DAEMON:-1}"
ATTACH_WHEN_NOT_READY="${SERIALWRAP_ATTACH_WHEN_NOT_READY:-1}"
ATTACH_WAIT_TICKS="${SERIALWRAP_ATTACH_WAIT_TICKS:-60}"
PREFERRED_COM="${SERIALWRAP_PREFERRED_COM:-COM0}"
MINICOM_BIN="${MINICOM_BIN:-/usr/bin/minicom}"
MINICOM_DEFAULT_COLOR="${MINICOM_DEFAULT_COLOR:-on}"
MINICOM_AUTO_CAPTURE="${MINICOM_AUTO_CAPTURE:-1}"
BLOG_DIR="${BLOG_DIR:-${BUILD_LOG_PATH:-${HOME}/arc_prj/b-log}}"

selector=""
if [[ $# -gt 0 && "${1}" != -* ]]; then
  selector="${1}"
  shift
fi

# 從剩餘參數中抽出 -D（使用者指定的裝置路徑），避免與 router 選取的 vtty 重複傳入 minicom
user_device=""
if [[ $# -gt 0 ]]; then
  _args_copy=("$@")
  _kept=()
  _i=0
  while [[ $_i -lt ${#_args_copy[@]} ]]; do
    if [[ "${_args_copy[$_i]}" == "-D" && $((_i+1)) -lt ${#_args_copy[@]} ]]; then
      user_device="${_args_copy[$((_i+1))]}"
      ((_i+=2))
    elif [[ "${_args_copy[$_i]}" == -D?* ]]; then
      user_device="${_args_copy[$_i]#-D}"
      ((_i+=1))
    else
      _kept+=("${_args_copy[$_i]}")
      ((_i+=1))
    fi
  done
  if [[ ${#_kept[@]} -gt 0 ]]; then
    set -- "${_kept[@]}"
  else
    set --
  fi
fi

_get_sessions_json() {
  "${SERIALWRAP_BIN}" --socket "${SOCKET}" session list 2>/dev/null || true
}

_json_ok() {
  local obj="$1"
  [[ "$(printf '%s' "${obj}" | jq -r '.ok // false')" == "true" ]]
}

_find_row_by_selector() {
  local obj="$1"
  local sel="$2"
  printf '%s' "${obj}" | jq -c --arg sel "${sel}" '.sessions[]? | select(.alias==$sel or .com==$sel or .session_id==$sel)' | head -n 1
}

_find_first_ready_row() {
  local obj="$1"
  printf '%s' "${obj}" | jq -c '.sessions[]? | select(.state=="READY" and (.vtty // "") != "")' | head -n 1
}

_find_row_by_vtty() {
  local obj="$1"
  local vtty="$2"
  printf '%s' "${obj}" | jq -c --arg v "${vtty}" '.sessions[]? | select(.vtty==$v)' | head -n 1
}

_find_attach_selector_default() {
  local obj="$1"
  local pick
  pick="$(printf '%s' "${obj}" | jq -r --arg com "${PREFERRED_COM}" '.sessions[]? | select(.com==$com) | .com' | head -n 1)"
  if [[ -n "${pick}" ]]; then
    printf '%s' "${pick}"
    return 0
  fi
  pick="$(printf '%s' "${obj}" | jq -r '.sessions[]?.com' | head -n 1)"
  printf '%s' "${pick}"
}

_exec_minicom() {
  local device="$1"
  local com_name="$2"
  shift 2

  local -a user_args
  user_args=("$@")
  local has_capture=0
  local has_color=0
  local i=0
  local arg=""

  while [[ $i -lt ${#user_args[@]} ]]; do
    arg="${user_args[$i]}"
    if [[ "${arg}" == "-C" || "${arg}" == --capturefile || "${arg}" == --capturefile=* || "${arg}" == -C* ]]; then
      has_capture=1
    fi
    if [[ "${arg}" == "-c" || "${arg}" == --color || "${arg}" == --color=* || "${arg}" == -c* ]]; then
      has_color=1
    fi
    ((i += 1))
  done

  local -a extra_args
  extra_args=()
  if [[ "${has_color}" -eq 0 && -n "${MINICOM_DEFAULT_COLOR}" ]]; then
    extra_args+=("--color=${MINICOM_DEFAULT_COLOR}")
  fi
  if [[ "${has_capture}" -eq 0 && "${MINICOM_AUTO_CAPTURE}" == "1" ]]; then
    mkdir -p "${BLOG_DIR}"
    local ts
    ts="$(date +%y%m%d-%H%M%S)"
    local safe_com
    safe_com="$(printf '%s' "${com_name}" | tr -c 'A-Za-z0-9._+-' '_')"
    local logfile="${BLOG_DIR}/mini_${safe_com}_${ts}.log"
    extra_args+=("-C" "${logfile}")
  fi

  exec "${MINICOM_BIN}" -D "${device}" "${extra_args[@]}" "${user_args[@]}"
}

_try_exec_row() {
  local row="$1"
  shift
  if [[ -z "${row}" ]]; then
    return 1
  fi
  local st
  local vtty
  local com_name
  st="$(printf '%s' "${row}" | jq -r '.state // ""')"
  vtty="$(printf '%s' "${row}" | jq -r '.vtty // ""')"
  com_name="$(printf '%s' "${row}" | jq -r '.com // "COMX"')"
  if [[ "${st}" == "READY" && -n "${vtty}" && -e "${vtty}" ]]; then
    _exec_minicom "${vtty}" "${com_name}" "$@"
  fi
  return 1
}

_try_attach_and_exec() {
  local sel="$1"
  shift
  local obj=""
  local row=""
  "${SERIALWRAP_BIN}" --socket "${SOCKET}" session attach --selector "${sel}" >/dev/null 2>&1 || true
  for _ in $(seq 1 "${ATTACH_WAIT_TICKS}"); do
    obj="$(_get_sessions_json)"
    if ! _json_ok "${obj}"; then
      sleep 0.2
      continue
    fi
    row="$(_find_row_by_selector "${obj}" "${sel}")"
    if _try_exec_row "${row}" "$@"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

state_json="$(_get_sessions_json)"

if [[ -z "${state_json}" ]] || ! _json_ok "${state_json}"; then
  if [[ "${AUTO_START_DAEMON}" == "1" ]]; then
    "${SERIALWRAP_BIN}" --socket "${SOCKET}" daemon start --profile-dir "${PROFILE_DIR}" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      state_json="$(_get_sessions_json)"
      if _json_ok "${state_json}"; then
        break
      fi
      sleep 0.1
    done
  fi
fi

if _json_ok "${state_json}"; then
  # user_device 為 /dev/pts/X → 以 vtty 直接查找對應 session
  if [[ -n "${user_device}" && "${user_device}" == /dev/pts/* ]]; then
    session_row="$(_find_row_by_vtty "${state_json}" "${user_device}")"
    if _try_exec_row "${session_row}" "$@"; then
      exit 0
    fi
    # 找到 session 但尚未 READY → 改以 com 作為 selector 走後續 attach 流程
    if [[ -n "${session_row}" && -z "${selector}" ]]; then
      selector="$(printf '%s' "${session_row}" | jq -r '.com // ""')"
    fi
  fi

  if [[ -n "${selector}" ]]; then
    session_row="$(_find_row_by_selector "${state_json}" "${selector}")"
    if _try_exec_row "${session_row}" "$@"; then
      exit 0
    fi
    if [[ "${ATTACH_WHEN_NOT_READY}" == "1" && -n "${session_row}" ]]; then
      _try_attach_and_exec "${selector}" "$@" || true
    fi
  else
    session_row="$(_find_first_ready_row "${state_json}")"
    if _try_exec_row "${session_row}" "$@"; then
      exit 0
    fi
    if [[ "${ATTACH_WHEN_NOT_READY}" == "1" ]]; then
      attach_selector="$(_find_attach_selector_default "${state_json}")"
      if [[ -n "${attach_selector}" ]]; then
        _try_attach_and_exec "${attach_selector}" "$@" || true
      fi
    fi
  fi
fi

if [[ -n "${user_device:-}" ]]; then
  _exec_minicom "${user_device}" "DIRECT" "$@"
fi

if [[ -n "${MINICOM_RAW_DEVICE:-}" ]]; then
  _exec_minicom "${MINICOM_RAW_DEVICE}" "RAW" "$@"
fi

echo "minicom_router: broker not ready, no READY session, and MINICOM_RAW_DEVICE not set" >&2
if [[ "$(printf '%s' "${state_json}" | jq -r '.ok // false')" == "true" ]]; then
  echo "sessions:" >&2
  printf '%s' "${state_json}" | jq -r '.sessions[]? | "  - \(.com) \(.alias) state=\(.state) last_error=\(.last_error // "-")"' >&2 || true
fi
echo "hint: ${SERIALWRAP_BIN} daemon start --profile-dir ${PROFILE_DIR}" >&2
echo "hint: ${SERIALWRAP_BIN} session list" >&2
exit 2
