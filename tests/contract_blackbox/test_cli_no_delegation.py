"""P4-T14 委派集合清空的黑箱守門（計畫 Task 14 步驟 1）。

每個管理群組都必須由 Rust 原生處理，不得經 Python 委派。黑箱 bundle（`mgmt_bundle`，
`mkbundle.sh --dev --delegate-sentinel`）的 `python_cli_delegate.entry` 指向 sentinel：被呼叫即寫入
`DELEGATED` marker 並 exit 97；任何漏網委派都會讓本檔失敗（Review Focus 2）。

紅燈階段：P3 委派集合仍含 7 群組（daemon、connect、benches、supervision-mode、service、setup、doctor），
這些探測以 `xfail(strict=True)` 標示；Task 14 清空 `DELEGATED_GROUPS` 的同一 commit 必須移除 marker。
`remote status` 與 `session console-attach／console-detach` 在 P3 已原生，不標 xfail。

只在 Rust 輪執行（`rust_only`）；執行的 CLI 為 mgmt_bundle 內的 `bin/serialwrap`，不依賴外部 `SERIALWRAP_CLI_EXE`。
"""
from __future__ import annotations

import subprocess

import pytest

# 全部 20 個頂層群組的代表探測中，會被委派或本就原生的命令；`--help`／`--version` 不列入（不經委派路由）。
GROUP_PROBES: list[list[str]] = [
    ["daemon", "status"],
    ["service", "status"],
    ["supervision-mode"],
    ["remote", "status"],
    ["benches"],
    ["connect", "NOPE"],
    ["setup", "--on-demand"],
    ["doctor"],
    ["session", "console-attach", "--selector", "COM0"],
    ["session", "console-detach", "--selector", "COM0", "--client-id", "c1"],
]

# P3 委派集合（DELEGATED_GROUPS）中的群組；Task 14 清空後不再需要 xfail。
STILL_DELEGATED_TODAY = {"daemon", "connect", "benches", "supervision-mode", "service", "setup", "doctor"}


def _probe_param(argv: list[str]):
    marks = []
    if argv[0] in STILL_DELEGATED_TODAY:
        marks.append(
            pytest.mark.xfail(
                strict=True,
                reason="P4-T14 紅燈：群組仍委派 Python（DELEGATED_GROUPS 尚未清空）",
            )
        )
    return pytest.param(argv, id=" ".join(argv), marks=marks)


@pytest.mark.parametrize("argv", [_probe_param(a) for a in GROUP_PROBES])
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
