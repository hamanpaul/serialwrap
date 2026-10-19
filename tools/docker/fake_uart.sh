#!/usr/bin/env bash
set -euo pipefail

ROOT="${SW_FAKE_UART_ROOT:-/run/sw-fake}"
BY_ID_DIR="${ROOT}/by-id"
PEER_DIR="${ROOT}"
PORT_ID="${SW_FAKE_UART_ID:-0}"
LINK="${BY_ID_DIR}/usb-fake-${PORT_ID}"
PEER_LINK="${PEER_DIR}/peer-${PORT_ID}"
PID_FILE="${ROOT}/socat-${PORT_ID}.pid"
LOG_FILE="${ROOT}/socat-${PORT_ID}.log"

usage() {
    echo "用法：$0 start|stop|status" >&2
}

wait_for_links() {
    local attempt
    for attempt in {1..50}; do
        if [[ -L "${LINK}" && -e "${LINK}" && -L "${PEER_LINK}" && -e "${PEER_LINK}" ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

start_pair() {
    command -v socat >/dev/null 2>&1 || { echo "找不到 socat" >&2; return 2; }
    mkdir -p "${BY_ID_DIR}"
    if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
        wait_for_links || { echo "既有 PTY pair 的 link 未就緒" >&2; return 1; }
        printf '%s\n' "${LINK} ${PEER_LINK}"
        return 0
    fi
    rm -f "${LINK}" "${PEER_LINK}" "${PID_FILE}"
    socat -d -d \
        "pty,raw,echo=0,link=${LINK}" \
        "pty,raw,echo=0,link=${PEER_LINK}" \
        >"${LOG_FILE}" 2>&1 &
    local pid=$!
    printf '%s\n' "${pid}" >"${PID_FILE}"
    if ! wait_for_links; then
        kill "${pid}" 2>/dev/null || true
        wait "${pid}" 2>/dev/null || true
        rm -f "${LINK}" "${PEER_LINK}" "${PID_FILE}"
        echo "PTY pair 未在 5 秒內就緒" >&2
        return 1
    fi
    printf '%s\n' "${LINK} ${PEER_LINK}"
}

stop_pair() {
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid="$(cat "${PID_FILE}")"
        if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
            kill -TERM "${pid}" 2>/dev/null || true
            for _ in {1..50}; do
                kill -0 "${pid}" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "${pid}" 2>/dev/null; then
                kill -KILL "${pid}" 2>/dev/null || true
            fi
        fi
    fi
    rm -f "${LINK}" "${PEER_LINK}" "${PID_FILE}"
}

case "${1:-}" in
    start) start_pair ;;
    stop) stop_pair ;;
    status)
        if wait_for_links && [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            printf 'ready %s %s\n' "${LINK}" "${PEER_LINK}"
        else
            echo "stopped"
            exit 1
        fi
        ;;
    *) usage; exit 2 ;;
esac
