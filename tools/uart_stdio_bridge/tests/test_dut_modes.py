"""DUT 執行模式（Docker image `-i` 無 `-t`、local-process）的失敗測試（Task 6 T6-1）。

只依 Task 6 Interfaces 撰寫，不窺探實作。被測符號尚未存在時，`_symbol` 以
pytest.fail 寫出「尚未實作」訊息，使紅燈原因是缺少行為，而非 collection 錯誤；
本檔本身可完整收集。需要 docker 的測試位於 test_docker_dut.py。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = TOOL_ROOT / "src"
DOCKERFILE = TOOL_ROOT / "docker" / "Dockerfile.dut"
DUT_MODES = "uart_stdio_bridge.bridge.dut_modes"
TASK = "Task 6 I6-1"

# 與 Task 4 `TargetConfig` 的預設 boot_banner 相同（plan 固定值）
DUT_BANNER = b"boot done\r\nroot@prplOS:/# "

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _symbol(module: str, attr: str, task: str = TASK):
    """取得待實作符號；尚不存在時以明確訊息失敗。"""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        missing = f"模組 {module} 不存在（{task}；缺少 {exc.name}）"
    else:
        if hasattr(mod, attr):
            return getattr(mod, attr)
        missing = f"{module}.{attr} 不存在（{task}）"
    pytest.fail(f"尚未實作：{missing}", pytrace=False)


def _read_exact(stream, n: int, timeout_s: float) -> bytes:
    """在限時內讀滿 n bytes（跨平台：以執行緒包住 os.read，避免 select 不支援 pipe）。"""
    buf = bytearray()

    def worker() -> None:
        while len(buf) < n:
            chunk = os.read(stream.fileno(), n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        pytest.fail(
            f"逾時：{timeout_s}s 內未讀到 {n} bytes，已讀 {bytes(buf)!r}",
            pytrace=False,
        )
    return bytes(buf)


def _default_target_json() -> dict:
    target_cls = _symbol("uart_stdio_bridge.dut.model", "TargetConfig", "Task 4 I4-1")
    return target_cls().to_json()


# ---------------------------------------------------------------------------
# docker_argv：固定序列、無 TTY／無特權、per-generation 容器名
# ---------------------------------------------------------------------------


def test_docker_argv_exact() -> None:
    docker_argv = _symbol(DUT_MODES, "docker_argv")
    scenario, control = Path("/s"), Path("/c")

    argv = docker_argv(
        "img:1",
        run_id="r",
        generation=1,
        scenario_dir=scenario,
        control_dir=control,
        seed=7,
        has_plan=True,
        user="1000:1000",
    )
    assert argv == [
        "docker", "run", "--rm", "-i",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "64",
        "--memory", "256m",
        "--name", "sw-dut-r-g1",
        "--user", "1000:1000",
        "--mount", f"type=bind,src={scenario},dst=/scenario,readonly",
        "--mount", f"type=bind,src={control},dst=/control",
        "img:1",
        "--target-json", "/scenario/target.json",
        "--plan-json", "/scenario/plan.json",
        "--control-dir", "/control",
        "--seed", "7",
    ]

    # 省略 user 時（POSIX）預設為宿主 uid:gid，使容器可寫 bind-mount 控制目錄
    if os.name != "nt":
        default_argv = docker_argv(
            "img:1",
            run_id="r",
            generation=1,
            scenario_dir=scenario,
            control_dir=control,
            seed=7,
            has_plan=True,
        )
        assert default_argv[default_argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_docker_argv_no_tty_no_privilege() -> None:
    docker_argv = _symbol(DUT_MODES, "docker_argv")
    argv = docker_argv(
        "img:1",
        run_id="r",
        generation=1,
        scenario_dir=Path("/s"),
        control_dir=Path("/c"),
        seed=7,
        has_plan=True,
        user="1000:1000",
    )

    # 必須是 -i（stdin pipe）而非 -t（TTY）
    assert "-i" in argv
    for forbidden in ("-t", "--tty", "-it", "-ti", "--privileged", "--device", "--net=host", "--network=host"):
        assert forbidden not in argv, f"argv 不得含 {forbidden!r}：{argv}"
    for prev, cur in zip(argv, argv[1:]):
        assert (prev, cur) not in {("--network", "host"), ("--net", "host")}, f"argv 不得含 host 網路：{argv}"
    for token in argv:
        assert "docker.sock" not in token, f"argv 不得掛載 docker.sock：{token!r}"
        assert "/dev/tty" not in token, f"argv 不得含 /dev/tty：{token!r}"
        assert not token.startswith(("--privileged", "--device", "--tty")), f"argv 含特權／TTY 旗標：{token!r}"


def test_docker_argv_name_per_generation() -> None:
    docker_argv = _symbol(DUT_MODES, "docker_argv")
    argv = docker_argv(
        "img:1",
        run_id="r",
        generation=2,
        scenario_dir=Path("/s"),
        control_dir=Path("/c"),
        seed=7,
        has_plan=True,
        user="1000:1000",
    )
    assert argv[argv.index("--name") + 1] == "sw-dut-r-g2"


# ---------------------------------------------------------------------------
# local_process_argv：隔離旗標、無互動、實際啟動後 stdout 前綴為 banner
# ---------------------------------------------------------------------------


def test_local_process_argv_isolated(tmp_path: Path) -> None:
    local_process_argv = _symbol(DUT_MODES, "local_process_argv")

    target = tmp_path / "target.json"
    target.write_text(json.dumps(_default_target_json(), ensure_ascii=False), encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir()

    argv = local_process_argv(
        SRC_ROOT,
        target_json=target,
        plan_json=None,
        control_dir=control,
        seed=1,
    )
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-E", "-s"], f"必須以 -E -s 隔離環境與 user site：{argv[:3]}"
    assert "-i" not in argv, "local-process 不得含互動旗標 -i"

    stderr_path = tmp_path / "dut.stderr.log"
    with stderr_path.open("wb") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=str(tmp_path))
        try:
            got = _read_exact(proc.stdout, len(DUT_BANNER), timeout_s=30)
            assert got == DUT_BANNER, f"stdout 前綴應為 banner，實得 {got!r}（stderr 見 {stderr_path}）"
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Dockerfile.dut：固定 digest、只複製 DUT 模組
# ---------------------------------------------------------------------------


def test_dockerfile_pinned_digest() -> None:
    assert DOCKERFILE.is_file(), f"Dockerfile.dut 尚未實作：{DOCKERFILE}（{TASK}）"
    text = DOCKERFILE.read_text(encoding="utf-8")
    first = text.splitlines()[0]
    assert re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", first), (
        f"第一行必須為 digest 固定的 FROM，實得 {first!r}"
    )
    lowered = text.lower()
    assert "pip install" not in lowered, "Dockerfile.dut 不得含 pip install（測試途中不升級相依）"
    assert "apt-get" not in lowered, "Dockerfile.dut 不得含 apt-get"


def test_dockerfile_copies_only_dut() -> None:
    assert DOCKERFILE.is_file(), f"Dockerfile.dut 尚未實作：{DOCKERFILE}（{TASK}）"
    sources: list[str] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        tokens = line.split()
        if tokens and tokens[0].upper() == "COPY":
            args = [t for t in tokens[1:] if not t.startswith("--")]
            sources.append(args[0])

    assert sources, "Dockerfile.dut 必須至少含一個 COPY（DUT 模組）"
    allowed_file = "src/uart_stdio_bridge/__init__.py"
    allowed_dir = "src/uart_stdio_bridge/dut/"
    for src in sources:
        assert src == allowed_file or src.startswith(allowed_dir), (
            f"COPY 來源超出 DUT 範圍（不得含 bridge／比較器／產品程式碼）：{src!r}"
        )


# ---------------------------------------------------------------------------
# image_tag：以工具樹雜湊為後綴
# ---------------------------------------------------------------------------


def test_image_tag_tracks_tool_tree() -> None:
    image_tag = _symbol(DUT_MODES, "image_tag")
    tool_tree_sha256 = _symbol("uart_stdio_bridge.identity", "tool_tree_sha256", "Task 1 I1-1")

    tag = image_tag()
    prefix = "serialwrap-dut-sim:0.1.0-"
    assert tag.startswith(prefix), f"image tag 必須以 {prefix!r} 開頭，實得 {tag!r}"
    assert tag[len(prefix):] == tool_tree_sha256()[:12]
