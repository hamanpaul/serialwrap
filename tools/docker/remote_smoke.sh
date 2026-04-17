#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-serialwrap:remote-smoke}"
SUFFIX="${SUFFIX:-$$}"
NETWORK_NAME="${NETWORK_NAME:-serialwrap-remote-net-${SUFFIX}}"
REMOTE_NAME="${REMOTE_NAME:-sw-remote-a-${SUFFIX}}"
REMOTE_PORT="${REMOTE_PORT:-7777}"
REMOTE_ENDPOINT="tcp://${REMOTE_NAME}:${REMOTE_PORT}"
STATUS_FILE="$(mktemp /tmp/serialwrap-remote-status.XXXXXX.json)"

json_extract() {
  local path="$1"
  docker run --rm -i "${IMAGE_TAG}" python3 -c '
import json
import sys

path = sys.argv[1].split(".")
current = json.load(sys.stdin)
for part in path:
    if not isinstance(current, dict):
        raise SystemExit(1)
    current = current.get(part)
if current is None:
    raise SystemExit(1)
print(current)
' "${path}"
}

json_command_done() {
  docker run --rm -i "${IMAGE_TAG}" python3 -c '
import json
import sys

obj = json.load(sys.stdin)
command = obj.get("command") or {}
raise SystemExit(0 if command.get("status") == "done" else 1)
'
}

cleanup() {
  docker rm -f "${REMOTE_NAME}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
  rm -f "${STATUS_FILE}"
}
trap cleanup EXIT

echo "[serialwrap] build image: ${IMAGE_TAG}"
DOCKER_BUILDKIT=1 docker build --progress=plain -t "${IMAGE_TAG}" "${ROOT_DIR}"

echo "[serialwrap] create isolated bridge network: ${NETWORK_NAME}"
docker network create "${NETWORK_NAME}" >/dev/null

echo "[serialwrap] start remote daemon container: ${REMOTE_NAME}"
docker run -d \
  --name "${REMOTE_NAME}" \
  --network "${NETWORK_NAME}" \
  "${IMAGE_TAG}" \
  python3 /opt/serialwrap/tools/docker/remote_lab.py --listen-host 0.0.0.0 --tcp-port "${REMOTE_PORT}" >/dev/null

echo "[serialwrap] wait remote endpoint ready"
for _ in $(seq 1 30); do
  if docker run --rm \
      --network "${NETWORK_NAME}" \
      "${IMAGE_TAG}" \
      serialwrap --endpoint "${REMOTE_ENDPOINT}" daemon status >"${STATUS_FILE}" 2>/dev/null; then
    break
  fi
  sleep 1
done

if ! test -s "${STATUS_FILE}"; then
  echo "[serialwrap] remote endpoint not ready"
  docker logs "${REMOTE_NAME}" || true
  exit 1
fi

echo "[serialwrap] daemon status"
cat "${STATUS_FILE}"
echo

echo "[serialwrap] session list"
docker run --rm \
  --network "${NETWORK_NAME}" \
  "${IMAGE_TAG}" \
  serialwrap --endpoint "${REMOTE_ENDPOINT}" session list

echo "[serialwrap] submit remote command"
submit_json="$(docker run --rm \
  --network "${NETWORK_NAME}" \
  "${IMAGE_TAG}" \
  serialwrap --endpoint "${REMOTE_ENDPOINT}" cmd submit --selector COM0 --cmd 'uname -a')"
echo "${submit_json}"

cmd_id="$(printf '%s\n' "${submit_json}" | json_extract cmd_id)"

echo "[serialwrap] wait command done: ${cmd_id}"
done=0
last_status_json=""
for _ in $(seq 1 30); do
  last_status_json="$(docker run --rm \
    --network "${NETWORK_NAME}" \
    "${IMAGE_TAG}" \
    serialwrap --endpoint "${REMOTE_ENDPOINT}" cmd status --cmd-id "${cmd_id}")"
  echo "${last_status_json}"
  if printf '%s\n' "${last_status_json}" | json_command_done; then
    done=1
    break
  fi
  sleep 1
done

if [[ "${done}" != "1" ]]; then
  echo "[serialwrap] remote command did not finish in time"
  if [[ -n "${last_status_json}" ]]; then
    echo "${last_status_json}"
  fi
  exit 1
fi

echo "[serialwrap] remote smoke PASS"
