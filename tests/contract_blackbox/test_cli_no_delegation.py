"""P4-T14 委派集合清空的黑箱守門（計畫 Task 14 步驟 1）。

每個群組都必須由 Rust 原生處理，不得經 Python 委派。黑箱 bundle（`mgmt_bundle`，
`mkbundle.sh --dev --delegate-sentinel`）的 `python_cli_delegate.entry` 指向 sentinel：被呼叫即寫入
`DELEGATED` marker 並 exit 97；任何漏網委派都會讓本檔失敗（Review Focus 2）。

綠燈階段：P4-T14 清空 `DELEGATED_GROUPS` 後，全部 20 個頂層群組與 `--help`／`--version`
皆已原生承接，不得發生任何委派（exit 97）。

只在 Rust 輪執行（`rust_only`）；執行的 CLI 為 mgmt_bundle 內的 `bin/serialwrap`，不依賴外部 `SERIALWRAP_CLI_EXE`。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# 全部 20 個頂層群組與 --help／--version 的代表探測；全部由 Rust 原生承接，不經委派路由。
GROUP_PROBES: list[list[str]] = [
    ["--help"],
    ["--version"],
    ["daemon", "status"],
    ["device", "list"],
    ["session", "list"],
    ["alias", "list"],
    ["cmd", "submit", "--selector", "COM0", "echo 1"],
    ["stream", "status", "--selector", "COM0"],
    ["log", "status", "--selector", "COM0"],
    ["file", "push", "--selector", "COM0", "--local", "nonexistent", "--remote", "x"],
    ["wal", "tail", "--selector", "COM0"],
    ["mcu", "status", "--selector", "COM0"],
    ["remote", "status"],
    ["connect", "NOPE"],
    ["benches"],
    ["event", "tail"],
    ["supervision-mode"],
    ["service", "status"],
    ["setup", "--on-demand"],
    ["doctor"],
    ["profile", "test", "--profile", "prpl-template"],
    ["skill"],
    ["session", "console-attach", "--selector", "COM0"],
    ["session", "console-detach", "--selector", "COM0", "--client-id", "c1"],
]


@pytest.mark.parametrize("argv", [pytest.param(a, id=" ".join(a)) for a in GROUP_PROBES])
@pytest.mark.rust_only
@pytest.mark.process_rpc
def test_every_mgmt_group_is_native(env, stub_path, no_delegation, mgmt_bundle, argv):
    # 直接執行 sentinel bundle 的 CLI：委派目標即 sentinel，exit 97 代表漏網委派。
    r = subprocess.run(
        [str(mgmt_bundle.cli), *argv],
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert r.returncode != 97, r.stderr.decode("utf-8", errors="replace")
