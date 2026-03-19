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
BLOG_DIR="${BLOG_DIR:-${HOME}/b-log}"
MINICOM_CAPTURE_WRAPPER="${MINICOM_CAPTURE_WRAPPER:-1}"

_shell_join() {
  local -a quoted
  quoted=()
  local arg
  for arg in "$@"; do
    printf -v arg '%q' "${arg}"
    quoted+=("${arg}")
  done
  local IFS=' '
  printf '%s' "${quoted[*]}"
}

selector=""
if [[ $# -gt 0 && "${1}" != -* ]]; then
  selector="${1}"
  shift
fi

user_device=""
if [[ $# -gt 0 ]]; then
  _args_copy=("$@")
  _kept=()
  _i=0
  while [[ $_i -lt ${#_args_copy[@]} ]]; do
    if [[ "${_args_copy[$_i]}" == "-D" && $((_i + 1)) -lt ${#_args_copy[@]} ]]; then
      user_device="${_args_copy[$((_i + 1))]}"
      ((_i += 2))
    elif [[ "${_args_copy[$_i]}" == -D?* ]]; then
      user_device="${_args_copy[$_i]#-D}"
      ((_i += 1))
    else
      _kept+=("${_args_copy[$_i]}")
      ((_i += 1))
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
  printf '%s' "${obj}" | jq -c '.sessions[]? | select(.state=="READY")' | head -n 1
}

_find_first_console_row() {
  local obj="$1"
  local row
  row="$(_find_first_ready_row "${obj}")"
  if [[ -n "${row}" ]]; then
    printf '%s' "${row}"
    return 0
  fi
  printf '%s' "${obj}" | jq -c '.sessions[]? | select(.state=="ATTACHED")' | head -n 1
}

_find_attach_selector_default() {
  local obj="$1"
  local pick
  pick="$(printf '%s' "${obj}" | jq -r --arg com "${PREFERRED_COM}" '.sessions[]? | select(.com==$com) | .com' | head -n 1)"
  if [[ -n "${pick}" ]]; then
    printf '%s' "${pick}"
    return 0
  fi
  printf '%s' "${obj}" | jq -r '.sessions[]?.com' | head -n 1
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

  local logfile=""
  if [[ "${has_capture}" -eq 0 && "${MINICOM_AUTO_CAPTURE}" == "1" ]]; then
    mkdir -p "${BLOG_DIR}"
    local ts
    ts="$(date +%y%m%d-%H%M%S)"
    local safe_com
    safe_com="$(printf '%s' "${com_name}" | tr -c 'A-Za-z0-9._+-' '_')"
    logfile="${BLOG_DIR}/mini_${safe_com}_${ts}.log"
  fi

  local -a cmd
  cmd=("${MINICOM_BIN}" -D "${device}" "${extra_args[@]}" "${user_args[@]}")
  if [[ -n "${logfile}" && "${MINICOM_CAPTURE_WRAPPER}" == "1" ]] && command -v script >/dev/null 2>&1; then
    local cmdline
    cmdline="$(_shell_join "${cmd[@]}")"
    exec script -qef -c "${cmdline}" "${logfile}"
  fi
  if [[ -n "${logfile}" ]]; then
    cmd+=("-C" "${logfile}")
  fi
  exec "${cmd[@]}"
}

_ensure_daemon() {
  local state_json
  state_json="$(_get_sessions_json)"
  if _json_ok "${state_json}"; then
    printf '%s' "${state_json}"
    return 0
  fi
  if [[ "${AUTO_START_DAEMON}" != "1" ]]; then
    printf '%s' "${state_json}"
    return 0
  fi
  "${SERIALWRAP_BIN}" --socket "${SOCKET}" daemon start --profile-dir "${PROFILE_DIR}" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    state_json="$(_get_sessions_json)"
    if _json_ok "${state_json}"; then
      break
    fi
    sleep 0.1
  done
  printf '%s' "${state_json}"
}

_wait_console_row() {
  local sel="$1"
  local row=""
  local state_json=""

  for _ in $(seq 1 "${ATTACH_WAIT_TICKS}"); do
    state_json="$(_get_sessions_json)"
    if ! _json_ok "${state_json}"; then
      sleep 0.2
      continue
    fi
    row="$(_find_row_by_selector "${state_json}" "${sel}")"
    if [[ -n "${row}" ]]; then
      local state
      state="$(printf '%s' "${row}" | jq -r '.state // ""')"
      if [[ "${state}" == "READY" || "${state}" == "ATTACHED" ]]; then
        printf '%s' "${row}"
        return 0
      fi
    fi
    sleep 0.2
  done
  return 1
}

_wait_ready_row() {
  local sel="$1"
  local row=""
  row="$(_wait_console_row "${sel}")" || return 1
  if [[ "$(printf '%s' "${row}" | jq -r '.state // ""')" == "READY" ]]; then
      printf '%s' "${row}"
      return 0
  fi
  return 1
}

_attach_console_json() {
  local sel="$1"
  "${SERIALWRAP_BIN}" --socket "${SOCKET}" session console-attach --selector "${sel}" --label "minicom:$$" 2>/dev/null || true
}

_run_broker_minicom() {
  local row="$1"
  shift
  local sel
  local com_name
  local attach_json
  local client_id
  local vtty

  sel="$(printf '%s' "${row}" | jq -r '.com // .session_id')"
  com_name="$(printf '%s' "${row}" | jq -r '.com // "COMX"')"
  attach_json="$(_attach_console_json "${sel}")"
  if [[ "$(printf '%s' "${attach_json}" | jq -r '.ok // false')" != "true" ]]; then
    return 1
  fi
  client_id="$(printf '%s' "${attach_json}" | jq -r '.client_id // ""')"
  vtty="$(printf '%s' "${attach_json}" | jq -r '.vtty // ""')"
  if [[ -z "${client_id}" || -z "${vtty}" ]]; then
    return 1
  fi

  cleanup() {
    "${SERIALWRAP_BIN}" --socket "${SOCKET}" session console-detach --selector "${sel}" --client-id "${client_id}" >/dev/null 2>&1 || true
  }
  trap cleanup EXIT INT TERM
  _exec_minicom "${vtty}" "${com_name}" "$@"
  local rc=$?
  trap - EXIT INT TERM
  cleanup
  return "${rc}"
}

state_json="$(_ensure_daemon)"

if _json_ok "${state_json}"; then
  if [[ -z "${selector}" ]]; then
    row="$(_find_first_console_row "${state_json}")"
    if [[ -z "${row}" && "${ATTACH_WHEN_NOT_READY}" == "1" ]]; then
      selector="$(_find_attach_selector_default "${state_json}")"
      if [[ -n "${selector}" ]]; then
        "${SERIALWRAP_BIN}" --socket "${SOCKET}" session attach --selector "${selector}" >/dev/null 2>&1 || true
        row="$(_wait_console_row "${selector}")"
      fi
    fi
  else
    row="$(_find_row_by_selector "${state_json}" "${selector}")"
    if [[ -n "${row}" && "$(printf '%s' "${row}" | jq -r '.state // ""')" != "READY" && "$(printf '%s' "${row}" | jq -r '.state // ""')" != "ATTACHED" && "${ATTACH_WHEN_NOT_READY}" == "1" ]]; then
      "${SERIALWRAP_BIN}" --socket "${SOCKET}" session attach --selector "${selector}" >/dev/null 2>&1 || true
      row="$(_wait_console_row "${selector}")"
    fi
  fi

  if [[ -n "${row:-}" ]]; then
    state="$(printf '%s' "${row}" | jq -r '.state // ""')"
  else
    state=""
  fi
  if [[ -n "${row:-}" && ( "${state}" == "READY" || "${state}" == "ATTACHED" ) ]]; then
    _run_broker_minicom "${row}" "$@"
    exit $?
  fi
fi

if [[ -n "${user_device:-}" ]]; then
  _exec_minicom "${user_device}" "DIRECT" "$@"
  exit $?
fi

if [[ -n "${MINICOM_RAW_DEVICE:-}" ]]; then
  _exec_minicom "${MINICOM_RAW_DEVICE}" "RAW" "$@"
  exit $?
fi

echo "minicom_router: broker not ready, no READY/ATTACHED session, and MINICOM_RAW_DEVICE not set" >&2
if [[ "$(printf '%s' "${state_json:-}" | jq -r '.ok // false' 2>/dev/null || printf 'false')" == "true" ]]; then
  echo "sessions:" >&2
  printf '%s' "${state_json}" | jq -r '.sessions[]? | "  - \(.com) \(.alias) state=\(.state) last_error=\(.last_error // "-")"' >&2 || true
fi
echo "hint: ${SERIALWRAP_BIN} daemon start --profile-dir ${PROFILE_DIR}" >&2
echo "hint: ${SERIALWRAP_BIN} session list" >&2
exit 2
