"""DUT 執行模式（Docker 與 local-process）命令列組裝與映像檔管理。"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

DUT_IMAGE_REPO: str = "serialwrap-dut-sim"


def image_tag() -> str:
    """依工具樹雜湊產生 DUT Docker image 標籤。"""
    from uart_stdio_bridge.identity import tool_tree_sha256

    return f"{DUT_IMAGE_REPO}:0.1.0-{tool_tree_sha256()[:12]}"


def docker_argv(
    image: str,
    *,
    run_id: str,
    generation: int,
    scenario_dir: pathlib.Path,
    control_dir: pathlib.Path,
    seed: int,
    has_plan: bool,
    user: str | None = None,
) -> list[str]:
    """組裝固定序列的 Docker 執行參數。

    強制使用 -i（無 -t）、隔離網路、唯讀根檔案系統、卸除所有 capabilities、
    限制 pids 與記憶體，以確保模擬環境乾淨與安全。
    """
    if user is None:
        if os.name != "nt":
            user_str = f"{os.getuid()}:{os.getgid()}"
        else:
            user_str = "65534:65534"
    else:
        user_str = user

    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--name",
        f"sw-dut-{run_id}-g{generation}",
        "--user",
        user_str,
        "--mount",
        f"type=bind,src={scenario_dir},dst=/scenario,readonly",
        "--mount",
        f"type=bind,src={control_dir},dst=/control",
        image,
        "--target-json",
        "/scenario/target.json",
    ]
    if has_plan:
        argv.extend(["--plan-json", "/scenario/plan.json"])
    argv.extend([
        "--control-dir",
        "/control",
        "--seed",
        str(seed),
    ])
    return argv


def local_process_argv(
    src_root: pathlib.Path,
    *,
    target_json: pathlib.Path,
    plan_json: pathlib.Path | None,
    control_dir: pathlib.Path,
    seed: int,
) -> list[str]:
    """組裝 local-process 執行模式的參數。

    以 -E -s 隔離環境變數與 user site，以確保不被宿主環境污染。
    """
    inline_code = (
        "import sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from uart_stdio_bridge.dut.__main__ import main; "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    argv = [
        sys.executable,
        "-E",
        "-s",
        "-c",
        inline_code,
        str(src_root),
        "--target-json",
        str(target_json),
    ]
    if plan_json:
        argv.extend(["--plan-json", str(plan_json)])
    argv.extend([
        "--control-dir",
        str(control_dir),
        "--seed",
        str(seed),
    ])
    return argv


def docker_image_id(image: str) -> str | None:
    """以 docker image inspect 取得容器 image ID，失敗回 None。"""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            return out if out else None
    except Exception:
        pass
    return None
