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

cmd_id="$(python3 - <<'PY' "${submit_json}"
import json
import sys
obj = json.loads(sys.argv[1])
cmd_id = obj.get("cmd_id")
if not cmd_id:
    raise SystemExit(1)
print(cmd_id)
PY
)"

echo "[serialwrap] wait command done: ${cmd_id}"
for _ in $(seq 1 30); do
  status_json="$(docker run --rm \
    --network "${NETWORK_NAME}" \
    "${IMAGE_TAG}" \
    serialwrap --endpoint "${REMOTE_ENDPOINT}" cmd status --cmd-id "${cmd_id}")"
  echo "${status_json}"
  if python3 - <<'PY' "${status_json}"
import json
import sys
obj = json.loads(sys.argv[1])
command = obj.get("command") or {}
raise SystemExit(0 if command.get("status") == "done" else 1)
PY
  then
    break
  fi
  sleep 1
done

echo "[serialwrap] remote smoke PASS"
