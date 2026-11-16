"""P7 Task 19：`p7_gate.sh` 兩個 Windows 列的 win-test.sh 參數可被解析（審查 P7-cd7acf0-5）。

`win-test.sh` 只在 `--` 之後接受 cargo 參數；以 `--dry-run`（插在 `--` 之前）執行解析階段，不需 Windows。
"""
from __future__ import annotations

import pathlib
import os
import re
import shlex
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


@pytest.mark.parametrize("var", ["WIN_NONCOM_ARGS", "WIN_COM_ARGS"])
def test_gate_win_test_argv_parses(var):
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\((.*)\)$", text, re.M)
    assert m, var
    args = shlex.split(m.group(1))
    cut = args.index("--")
    r = subprocess.run(["bash", str(ROOT / "scripts/rust/win-test.sh"), *args[:cut], "--dry-run", *args[cut:]],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (var, r.stderr)
    assert "未知參數" not in r.stderr, (var, r.stderr)
    if var == "WIN_COM_ARGS":
        assert "--test cap_windows_com_p7" in r.stdout, r.stdout


def test_gate_func_comparison_lookup_finds_nested_report(tmp_path):
    """func-test 列取 comparison.json 的那一行須找到 runner 的實際深度 report/<case>/bridge/<n>/（第 12 輪實跑：
    舊 glob 只到兩層，PASS 的 brcm-01-login-flow 被記成 rc 1）。"""
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    line = next(ln.strip() for ln in text.splitlines() if ln.strip().startswith('cmp="$('))
    nested = tmp_path / "report" / "brcm-01-login-flow" / "bridge" / "0"
    nested.mkdir(parents=True)
    (nested / "comparison.json").write_text("{}", encoding="utf-8")
    r = subprocess.run(["bash", "-c", f'd="{tmp_path}"; {line}; printf %s "$cmp"'], capture_output=True, text=True, timeout=30)
    assert r.stdout == str(nested / "comparison.json"), (r.stdout, r.stderr)


def test_gate_snapshots_artifacts_before_cargo_rebuilds(tmp_path):
    """受測 CLI／daemon 須在 cargo build 後、cargo L1 列（--features test-fakes 會重建 target/debug）之前快照到 run
    目錄，黑箱與 verify 皆引用快照（第 12 輪實跑：引用 target/debug 使 verify 的 artifact sha256 不符）。"""
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    assert 'RUST_D="$RUN/artifacts/serialwrapd"' in text and 'RUST_CLI="$RUN/artifacts/serialwrap"' in text
    build = text.index('CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --workspace --bins >')
    assert 'TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/target}"' in text
    assert 'CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --workspace --bins' in text
    assert 'CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --release -p sw-daemon -p sw-cli' in text
    snap = text.index('cp -p "$TARGET_DIR/debug/serialwrapd" "$RUST_D"')
    release = text.index('CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --release -p sw-daemon -p sw-cli')
    release_snap = text.index('cp -p "$TARGET_DIR/x86_64-unknown-linux-gnu/release/serialwrapd" "$PRIOR_RELEASE_D"')
    l1 = text.index('run_row "cargo test --workspace"')
    assert build < snap < release < release_snap < l1
    assert "--prior-slice-artifact serialwrapd_release" in text
    assert "--od59-contract" in text
    assert 'cp -p "$TARGET_DIR/debug/serialwrapd" "$RUST_D"' not in text[snap + 1:], \
        "debug artifact 快照之後不得再以 target/debug 的 daemon 取代 run 目錄快照"

    # 真正執行 gate 使用的 cp 命令：舊 default target 留有二進位時，快照仍須取本輪 custom target SHA。
    default_target = tmp_path / "default-target"
    build_target = tmp_path / "candidate-target"
    (default_target / "debug").mkdir(parents=True)
    (build_target / "debug").mkdir(parents=True)
    (default_target / "debug/serialwrapd").write_bytes(b"stale daemon")
    (build_target / "debug/serialwrapd").write_bytes(b"new daemon from this build")
    destination = tmp_path / "run/artifacts/serialwrapd"
    destination.parent.mkdir(parents=True)
    copy_line = next(line.strip() for line in text.splitlines()
                     if line.strip() == 'cp -p "$TARGET_DIR/debug/serialwrapd" "$RUST_D"')
    command = (f'TARGET_DIR={shlex.quote(str(build_target))}; RUST_D={shlex.quote(str(destination))}; '
               f'{copy_line}')
    copied = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=30)
    assert copied.returncode == 0, copied.stderr
    assert destination.read_bytes() == b"new daemon from this build"
    assert destination.read_bytes() != (default_target / "debug/serialwrapd").read_bytes()

    # 在不建置真實 cargo artifact 的隔離 fixture 內執行前置 build function，確認它恢復呼叫端目錄。
    fixture_root = tmp_path / "prereqs-repo"
    fixture_scripts = fixture_root / "scripts/rust"
    fixture_scripts.mkdir(parents=True)
    helper = ROOT / "scripts/rust/gate_prereqs.sh"
    (fixture_scripts / "gate_prereqs.sh").write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cargo_stub = fake_bin / "cargo"
    cargo_stub.write_text(
        "#!/usr/bin/env bash\nset -eu\ntarget=''\nwhile (($#)); do "
        "if [[ $1 == --target ]]; then target=$2; shift 2; else shift; fi; done\n"
        "mkdir -p \"$CARGO_TARGET_DIR/$target/debug\"; : >\"$CARGO_TARGET_DIR/$target/debug/serialwrap\"\n",
        encoding="utf-8",
    )
    cargo_stub.chmod(0o755)
    bundle_stub = fixture_scripts / "mkbundle.sh"
    bundle_stub.write_text(
        "#!/usr/bin/env bash\nset -eu\nout=''\nwhile (($#)); do "
        "if [[ $1 == --out ]]; then out=$2; shift 2; else shift; fi; done\n"
        "mkdir -p \"$out/bin\"; : >\"$out/bin/serialwrap\"\n",
        encoding="utf-8",
    )
    bundle_stub.chmod(0o755)
    caller_target = tmp_path / "caller-target"
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(caller_target)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    shell = (f'source {shlex.quote(str(fixture_scripts / "gate_prereqs.sh"))} --dry-run >/dev/null; '
             'sw_gate_prereqs_build >/dev/null; printf "%s" "$CARGO_TARGET_DIR"')
    result = subprocess.run(["bash", "-c", shell], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == str(caller_target), result.stdout
    ab_text = (ROOT / "scripts/rust/run_p7_ab.sh").read_text(encoding="utf-8")
    assert 'latency["samples_per_run"]' in ab_text and 'latency["repeats"]' in ab_text
    assert "P7_OD59_REQUIRE_CONTRACT=1" in ab_text and "od59-contract-a" in ab_text and "od59-contract-b" in ab_text


def test_com_row_is_od53_capability_set():
    """rulings/p7.md 第 6 輪 SPEC-GAP #5（適用 OD-53）：Windows COM 列為能力集合——test-binary 參數以
    `--ignored cap_windows_com__` 明確只執行 ignored 能力測試，且 `--caps windows_com` 實跑。以 L1 鎖 OD-24 防線的
    `_selector_explicit_caps` 推導（不另寫規則）：推導結果須恰為 `--caps` 指定的能力。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_l1_judge_tests", ROOT / "tests" / "test_l1_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    args = shlex.split(re.search(r"^WIN_COM_ARGS=\((.*)\)$", text, re.M).group(1))
    caps = [args[i + 1] for i, a in enumerate(args) if a == "--caps"]
    cut = args.index("--")
    packages = [x for i, a in enumerate(args[:cut]) if a == "--package" for x in (a, args[i + 1])]
    selector = {"argv": [*packages, *args[cut + 1:]], "markexpr": "", "paths": []}
    assert caps == ["windows_com"], args
    assert mod._selector_explicit_caps(selector) == caps, selector
    noncom = shlex.split(re.search(r"^WIN_NONCOM_ARGS=\((.*)\)$", text, re.M).group(1))
    assert mod._selector_explicit_caps({"argv": noncom}) == [], noncom      # non-COM 列仍是 --skip cap_，不是能力集合
