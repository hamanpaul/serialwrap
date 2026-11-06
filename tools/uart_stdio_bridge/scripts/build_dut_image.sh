#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${TOOL_ROOT}"

TAG=$(PYTHONPATH=src python3 -c 'from uart_stdio_bridge.bridge.dut_modes import image_tag; print(image_tag())')
docker build -f docker/Dockerfile.dut -t "${TAG}" .

IMAGE_ID=$(docker image inspect --format '{{.Id}}' "${TAG}")
python3 -c 'import json, sys; print(json.dumps({"image": sys.argv[1], "image_id": sys.argv[2]}, sort_keys=True, separators=(",", ":")))' "${TAG}" "${IMAGE_ID}"
