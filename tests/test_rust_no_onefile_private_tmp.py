"""`check_no_onefile_extract.py --private-tmp`：以私有 mount namespace 隔離 /tmp（S1X Y5，比照 OD-25 隔離精神）。

出口 run `s1exit-20261028T050554` 的報告判 INSUFFICIENT：共用 /tmp 有 63689 筆其他程序（多為 thala-git-*）
的建立事件無法歸屬 CLI。`--private-tmp` 讓 CLI 在 user+mount namespace 內執行，/tmp 為 bind 進來的
全新空目錄並宣告獨占：其中任何建立事件或殘留都歸屬 CLI 判 FAIL（包含無視 TMPDIR、寫死 /tmp 的解壓器，
比 OD-25 的環境變數隔離更嚴）；namespace 外的 /tmp 活動看不到也不計入。無法建立 namespace → INSUFFICIENT。

受測假 CLI 與工作目錄放在 /tmp 以外（namespace 內 /tmp 被替換）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_no_onefile_extract.py"


@pytest.fixture()
def outside_tmp():
    base = ROOT / "target"
    base.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="test-onefile-privtmp-", dir=str(base)))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _run(outside: Path, cli_text: str, *, runs: int = 2, extra_env: dict[str, str] | None = None,
         json_out: Path | None = None, cli_args: str = "") -> tuple[subprocess.CompletedProcess[str], dict | None]:
    fake = outside / "fake_cli.py"
    fake.write_text(cli_text, encoding="utf-8")
    cmd = [sys.executable, str(SCRIPT), "--cli", f"{sys.executable} {fake}", f"--cli-args={cli_args}",
           "--runs", str(runs), "--private-tmp", "--private-tmp-base", str(outside)]
    if json_out is not None:
        cmd += ["--json-out", str(json_out)]
    env = dict(os.environ)
    env.update(extra_env or {})
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    data = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
    return res, data


def _unshare_ok() -> bool:
    return shutil.which("unshare") is not None and subprocess.run(
        ["unshare", "-Um", "--map-current-user", "--keep-caps", "true"], capture_output=True).returncode == 0


needs_ns = pytest.mark.skipif(not _unshare_ok(), reason="需要非特權 user+mount namespace（證據不足，非 PASS）")


@needs_ns
def test_private_tmp_clean_cli_passes_and_declares_isolation(outside_tmp):
    res, data = _run(outside_tmp, "print('ok')\n")
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "PASS", data
    assert data["ok"] is True
    assert data["system_tmp_private"] is True
    assert data["tmp_isolation"] == "mount-ns-private-tmp"
    assert data["system_tmp"] == "/tmp"


@needs_ns
def test_private_tmp_hardcoded_tmp_write_fails(outside_tmp):
    """無視 TMPDIR、寫死 /tmp 的解壓：namespace 內的 /tmp 為獨占 → FAIL；真實 /tmp 不受影響。"""
    probe = f"sw-onefile-hardcoded-{os.getpid()}"
    cli = ("import os\n"
           f"d = '/tmp/{probe}'\n"
           "os.makedirs(d, exist_ok=True)\n"
           "open(os.path.join(d, 'payload.so'), 'w').write('x')\n")
    res, data = _run(outside_tmp, cli, runs=1)
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "FAIL", data
    assert any(probe in row["path"] for row in data["created"]), data["created"]
    assert any(probe in row["path"] for row in data["system_tmp_new"]), data["system_tmp_new"]
    assert not Path(f"/tmp/{probe}").exists()


@needs_ns
def test_private_tmp_create_then_delete_in_tmp_fails(outside_tmp):
    cli = ("import os\n"
           "p = '/tmp/sw-onefile-transient.exe'\n"
           "open(p, 'w').write('x')\n"
           "os.remove(p)\n")
    res, data = _run(outside_tmp, cli, runs=1)
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "FAIL", data
    assert any("sw-onefile-transient.exe" in row["path"] for row in data["created"]), data["created"]


@needs_ns
def test_private_tmp_ignores_activity_outside_namespace(outside_tmp):
    """namespace 外其他程序對真實 /tmp 的建立不得使結果變為 INSUFFICIENT（出口 run 的 thala-git-* 情境）。"""
    stop = threading.Event()
    noise = Path(tempfile.mkdtemp(prefix="sw-onefile-noise-", dir="/tmp"))

    def writer() -> None:
        i = 0
        while not stop.is_set():
            (noise / f"n{i}").write_text("x")
            i += 1
            stop.wait(0.005)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        res, data = _run(outside_tmp, "print('ok')\n", runs=5)
    finally:
        stop.set()
        t.join(timeout=5)
        shutil.rmtree(noise, ignore_errors=True)
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "PASS", data
    assert data["system_tmp_unattributed"] == []


@needs_ns
def test_private_tmp_json_out_written_outside_namespace(outside_tmp):
    out = outside_tmp / "report.json"
    res, data = _run(outside_tmp, "print('ok')\n", runs=1, json_out=out)
    assert data is not None, res.stdout + res.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == data


def test_private_tmp_without_unshare_is_insufficient(outside_tmp):
    """無法建立 namespace（PATH 找不到 unshare）→ 報告 INSUFFICIENT、ok false，不得退回共用 /tmp 判 PASS。"""
    bindir = outside_tmp / "bin"
    bindir.mkdir()
    res, data = _run(outside_tmp, "print('ok')\n", runs=1, extra_env={"PATH": str(bindir)})
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "INSUFFICIENT", data
    assert data["ok"] is False
    assert any("private_tmp" in r for r in data["insufficient_reasons"]), data


def test_private_tmp_rejects_cli_under_tmp(tmp_path):
    """受測 CLI 位於 /tmp 下時 namespace 內看不到 → 用法錯誤（exit 2），不得以「找不到 CLI」的非零退出混入判定。"""
    if not str(tmp_path).startswith("/tmp/"):
        pytest.skip("tmp_path 不在 /tmp 下")
    fake = tmp_path / "fake_cli.py"
    fake.write_text("print('ok')\n", encoding="utf-8")
    res = subprocess.run([sys.executable, str(SCRIPT), "--cli", str(fake), "--runs", "1", "--private-tmp"],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr


@pytest.fixture()
def under_tmp():
    d = Path(tempfile.mkdtemp(prefix="test-onefile-undertmp-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_private_tmp_rejects_cli_symlink_under_tmp(outside_tmp, under_tmp):
    """`--cli` 為 /tmp 下指向外部的 symlink：resolve 後在 /tmp 外，但 namespace 內看不到該 symlink → exit 2（bf8583b#1）。"""
    fake = outside_tmp / "fake_cli.py"
    fake.write_text("print('ok')\n", encoding="utf-8")
    link = under_tmp / "fake_cli.py"
    link.symlink_to(fake)
    res = subprocess.run([sys.executable, str(SCRIPT), "--cli", f"{sys.executable} {link}", "--runs", "1",
                          "--private-tmp", "--private-tmp-base", str(outside_tmp)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr
    assert str(link) in res.stderr, res.stderr


def test_private_tmp_rejects_interpreter_under_tmp(outside_tmp, under_tmp):
    """本工具以 /tmp 下的直譯器（symlink／venv）執行時，內層 `sys.executable` 在 namespace 內看不到 → exit 2（bf8583b#1）。"""
    fake = outside_tmp / "fake_cli.py"
    fake.write_text("print('ok')\n", encoding="utf-8")
    py = under_tmp / "python3"
    py.symlink_to(Path(sys.executable).resolve())
    res = subprocess.run([str(py), str(SCRIPT), "--cli", f"{Path(sys.executable).resolve()} {fake}", "--runs", "1",
                          "--private-tmp", "--private-tmp-base", str(outside_tmp)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr
    assert str(py) in res.stderr, res.stderr


def test_private_tmp_rejects_relative_cli_with_cwd_under_tmp(outside_tmp, under_tmp):
    """工作目錄位於 /tmp 下、`--cli` 以相對路徑給出時，namespace 內看不到 cwd → exit 2，不得以內層 CLI 非零退出判產品 FAIL（c46c416#1）。"""
    fake = under_tmp / "fake_cli.py"
    fake.write_text("print('ok')\n", encoding="utf-8")
    res = subprocess.run([sys.executable, str(SCRIPT), "--cli", f"{sys.executable} fake_cli.py", "--runs", "1",
                          "--private-tmp", "--private-tmp-base", str(outside_tmp)],
                         capture_output=True, text=True, timeout=60, cwd=str(under_tmp))
    assert res.returncode == 2, res.stdout + res.stderr
    assert str(under_tmp) in res.stderr, res.stderr


@needs_ns
def test_private_tmp_passes_dash_prefixed_cli_args_to_inner(outside_tmp):
    """預設 `--cli-args --version` 形式（值以 - 開頭）須原樣傳進 namespace 內層，CLI 收到該參數。"""
    cli = ("import sys\n"
           "sys.exit(0 if sys.argv[1:] == ['--version'] else 3)\n")
    res, data = _run(outside_tmp, cli, runs=1, cli_args="--version")
    assert data is not None, res.stdout + res.stderr
    assert data["verdict"] == "PASS", data
    assert data["argv"][-1] == "--version"
