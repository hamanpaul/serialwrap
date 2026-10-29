"""OD-50：`install.sh` 改為安裝 Rust bundle（下載 GitHub release 並以 SHA256SUMS 驗證，或 `--bundle` 離線安裝）。

以假 bundle（`bin/serialwrap` 為記錄參數並回傳指定 rc 的腳本）驅動真正的 `install.sh`：
- `--bundle` 目錄／tar.gz 兩種離線來源；
- 經 `SERIALWRAP_RELEASE_BASE_URL` 指向本機 http.server 的下載、`--version` 與 latest（redirect）解析；
- SHA256 不符、SHA256SUMS 缺列時不執行 setup；
- 三項系統需求（x86_64、glibc ≥ 2.39、/usr/bin/python3.12）各自不符時 rc=2 且未執行 setup；
- setup rc 透傳、`--` 之後參數原樣轉交、舊 pipx 安裝提示（不自動移除）、暫存目錄清除。
全部只連 127.0.0.1，不連外網。需求探測一律以環境變數覆寫成固定值，避免依賴 host 實際環境。
"""

from __future__ import annotations

import hashlib
import http.server
import io
import os
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

# 每個案例都以 bash 子行程執行 install.sh，部分另開本機 HTTP server。
pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
VERSION = "0.4.0"
TARBALL = f"serialwrap-{VERSION}-linux-x86_64.tar.gz"

FAKE_SETUP = """#!/usr/bin/env bash
# 假 bundle 入口：記錄收到的參數（每行一個）後回傳 FAKE_SETUP_RC
printf '%s\\n' "$@" > "$FAKE_SETUP_LOG"
echo '{"ok":true,"fake":true}'
exit "${FAKE_SETUP_RC:-0}"
"""

FAKE_PIPX = """#!/usr/bin/env bash
# 假 pipx：記錄呼叫並依 FAKE_PIPX_LIST 輸出 list --short
printf '%s\\n' "$*" >> "$FAKE_PIPX_LOG"
if [[ "$1" == list ]]; then
  printf '%s' "${FAKE_PIPX_LIST:-}"
fi
exit 0
"""


def _write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_bundle(root: Path) -> Path:
    """建立最小假 bundle：bin/serialwrap 與佔位的 versions/<版本>/。"""
    _write_exec(root / "bin" / "serialwrap", FAKE_SETUP)
    (root / "versions" / VERSION).mkdir(parents=True, exist_ok=True)
    (root / "versions" / VERSION / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    return root


def _tar_bytes(bundle: Path, *, top: str | None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(bundle.rglob("*")):
            rel = path.relative_to(bundle).as_posix()
            tar.add(path, arcname=f"{top}/{rel}" if top else rel, recursive=False)
    return buf.getvalue()


class _Env:
    """單一案例的隔離環境：HOME、TMPDIR、假 pipx、需求探測覆寫與 setup 紀錄檔。"""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.tmpdir = tmp / "tmpdir"
        self.tmpdir.mkdir()
        self.setup_log = tmp / "setup-args.txt"
        self.pipx_log = tmp / "pipx-calls.txt"
        self.stub_bin = tmp / "stub-bin"
        _write_exec(self.stub_bin / "pipx", FAKE_PIPX)
        self.python312 = _write_exec(tmp / "fake-python3.12", "#!/bin/sh\nexit 0\n")
        self.env = {
            "PATH": f"{self.stub_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "LANG": "C.UTF-8",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "FAKE_SETUP_LOG": str(self.setup_log),
            "FAKE_PIPX_LOG": str(self.pipx_log),
            "SERIALWRAP_INSTALL_ARCH": "x86_64",
            "SERIALWRAP_INSTALL_GLIBC": "2.39",
            "SERIALWRAP_INSTALL_PYTHON312": str(self.python312),
        }

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(extra)
        return subprocess.run(["bash", str(INSTALL_SH), *args], env=env, cwd=str(self.tmp),
                              capture_output=True, text=True, timeout=120)

    def setup_args(self) -> list[str] | None:
        if not self.setup_log.exists():
            return None
        return self.setup_log.read_text(encoding="utf-8").splitlines()

    def tmpdir_empty(self) -> bool:
        return not any(self.tmpdir.iterdir())


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path)


def _out(r: subprocess.CompletedProcess[str]) -> str:
    return r.stdout + r.stderr


# ---------- 本機 release server ----------

class _ReleaseServer:
    """模擬 https://github.com/<owner>/<repo>/releases：latest redirect、tag 頁與 download/<tag>/<檔名>。"""

    def __init__(self, latest: str | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.latest = latest
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server 介面
                outer.requests.append(self.path)
                if self.path == "/releases/latest" and outer.latest:
                    self.send_response(302)
                    self.send_header("Location", f"/releases/tag/{outer.latest}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.latest and self.path == f"/releases/tag/{outer.latest}":
                    body = b"<html>release page</html>"
                elif self.path in outer.files:
                    body = outer.files[self.path]
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 不汙染 pytest 輸出
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/releases"

    def publish(self, version: str, tarball: bytes, sums: str | None) -> None:
        prefix = f"/releases/download/v{version}"
        self.files[f"{prefix}/serialwrap-{version}-linux-x86_64.tar.gz"] = tarball
        if sums is not None:
            self.files[f"{prefix}/SHA256SUMS"] = sums.encode("utf-8")

    def __enter__(self) -> "_ReleaseServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _sums_for(name: str, data: bytes, *, extra: str = "") -> str:
    return f"{extra}{hashlib.sha256(data).hexdigest()}  {name}\n"


# ---------- 說明 ----------

def test_help_documents_usage_and_requirements(env):
    r = env.run("--help")
    assert r.returncode == 0, _out(r)
    for token in ("--version", "--bundle", "SERIALWRAP_RELEASE_BASE_URL", "x86_64", "2.39",
                  "/usr/bin/python3.12", "setup", "SHA256SUMS"):
        assert token in r.stdout, token
    assert env.setup_args() is None
    r2 = env.run("-h")
    assert r2.returncode == 0 and r2.stdout == r.stdout


# ---------- 離線：--bundle ----------

def test_bundle_directory_runs_setup_with_args(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    r = env.run("--bundle", str(bundle), "--user")
    assert r.returncode == 0, _out(r)
    assert env.setup_args() == ["setup", "--user"]
    assert env.tmpdir_empty()


@pytest.mark.parametrize("top", [None, f"serialwrap-{VERSION}-linux-x86_64"])
def test_bundle_tarball_is_extracted_then_setup(env, tmp_path, top):
    bundle = _make_bundle(tmp_path / "bundle")
    tarball = tmp_path / TARBALL
    tarball.write_bytes(_tar_bytes(bundle, top=top))
    r = env.run("--bundle", str(tarball), "--", "--on-demand")
    assert r.returncode == 0, _out(r)
    assert env.setup_args() == ["setup", "--on-demand"]
    assert env.tmpdir_empty(), list(env.tmpdir.iterdir())


def test_tarball_entry_not_executable_reports_tmpdir_hint(env, tmp_path):
    """解出的 bin/serialwrap 無法執行（例如 TMPDIR 掛載為 noexec）時明確提示改用 TMPDIR，不執行 setup。"""
    bundle = _make_bundle(tmp_path / "bundle")
    (bundle / "bin" / "serialwrap").chmod(0o644)
    tarball = tmp_path / TARBALL
    tarball.write_bytes(_tar_bytes(bundle, top=None))
    r = env.run("--bundle", str(tarball))
    assert r.returncode == 2, _out(r)
    assert "TMPDIR" in r.stderr
    assert env.setup_args() is None
    assert env.tmpdir_empty()


def test_bundle_without_entry_fails_before_setup(env, tmp_path):
    (tmp_path / "empty-bundle").mkdir()
    r = env.run("--bundle", str(tmp_path / "empty-bundle"))
    assert r.returncode == 2, _out(r)
    assert "bin/serialwrap" in _out(r)
    assert env.setup_args() is None


def test_bundle_and_version_are_exclusive(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    r = env.run("--bundle", str(bundle), "--version", VERSION)
    assert r.returncode == 2, _out(r)
    assert env.setup_args() is None


@pytest.mark.parametrize("bad", ["latest", "0.4", "v0.4.0.1", "0.4.0;rm"])
def test_invalid_version_rejected(env, bad):
    r = env.run("--version", bad)
    assert r.returncode == 2, _out(r)
    assert env.setup_args() is None


# ---------- 參數轉交與 rc 透傳 ----------

def test_args_after_double_dash_passed_verbatim(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    r = env.run("--bundle", str(bundle), "--", "--version", "--bundle", "a b", "--", "-h")
    assert r.returncode == 0, _out(r)
    assert env.setup_args() == ["setup", "--version", "--bundle", "a b", "--", "-h"]


@pytest.mark.parametrize("rc", [1, 3, 7])
def test_setup_rc_is_propagated(env, tmp_path, rc):
    bundle = _make_bundle(tmp_path / "bundle")
    (env.home / ".local" / "pipx" / "venvs" / "serialwrap").mkdir(parents=True)
    r = env.run("--bundle", str(bundle), FAKE_SETUP_RC=str(rc))
    assert r.returncode == rc, _out(r)
    assert env.setup_args() == ["setup"]
    # setup 失敗時不提示移除舊 pipx（入口未必已由 bundle 接管）
    assert "pipx uninstall serialwrap" not in _out(r)
    assert env.tmpdir_empty()


# ---------- 下載 ----------

def test_download_explicit_version(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    data = _tar_bytes(bundle, top=f"serialwrap-{VERSION}-linux-x86_64")
    with _ReleaseServer() as srv:
        srv.publish(VERSION, data, _sums_for("other-file.zip", b"x") + _sums_for(TARBALL, data))
        r = env.run("--version", VERSION, "--user", SERIALWRAP_RELEASE_BASE_URL=srv.base)
    assert r.returncode == 0, _out(r)
    assert env.setup_args() == ["setup", "--user"]
    assert f"/releases/download/v{VERSION}/{TARBALL}" in srv.requests
    assert f"/releases/download/v{VERSION}/SHA256SUMS" in srv.requests
    assert "/releases/latest" not in srv.requests
    assert env.tmpdir_empty()


@pytest.mark.parametrize("downloader", ["curl", "wget"])
def test_download_latest_resolves_redirect(env, tmp_path, downloader):
    if shutil.which(downloader) is None:
        pytest.skip(f"cap_missing:{downloader}：host 無 {downloader}（證據不足，非 PASS）")
    latest = "0.4.1"
    bundle = _make_bundle(tmp_path / "bundle")
    data = _tar_bytes(bundle, top=None)
    with _ReleaseServer(latest=f"v{latest}") as srv:
        srv.publish(latest, data, _sums_for(f"serialwrap-{latest}-linux-x86_64.tar.gz", data))
        r = env.run(SERIALWRAP_RELEASE_BASE_URL=srv.base, SERIALWRAP_INSTALL_DOWNLOADER=downloader)
    assert r.returncode == 0, _out(r)
    assert env.setup_args() == ["setup"]
    assert "/releases/latest" in srv.requests
    assert f"/releases/download/v{latest}/serialwrap-{latest}-linux-x86_64.tar.gz" in srv.requests
    assert latest in _out(r)
    assert env.tmpdir_empty()


def test_sha256_mismatch_does_not_run_setup(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    data = _tar_bytes(bundle, top=None)
    with _ReleaseServer() as srv:
        srv.publish(VERSION, data, f"{'0' * 64}  {TARBALL}\n")
        r = env.run("--version", VERSION, SERIALWRAP_RELEASE_BASE_URL=srv.base)
    assert r.returncode != 0, _out(r)
    assert "SHA256" in _out(r)
    assert env.setup_args() is None
    assert env.tmpdir_empty()


def test_missing_sha256sums_row_fails(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    data = _tar_bytes(bundle, top=None)
    with _ReleaseServer() as srv:
        # 只有名稱相近的列（前綴／後綴不同）不算：必須逐欄位完全相符
        srv.publish(VERSION, data, _sums_for(TARBALL + ".sig", data) + _sums_for("x" + TARBALL, data))
        r = env.run("--version", VERSION, SERIALWRAP_RELEASE_BASE_URL=srv.base)
    assert r.returncode != 0, _out(r)
    assert "SHA256SUMS" in _out(r)
    assert env.setup_args() is None
    assert env.tmpdir_empty()


def test_missing_sha256sums_file_fails(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    data = _tar_bytes(bundle, top=None)
    with _ReleaseServer() as srv:
        srv.publish(VERSION, data, None)
        r = env.run("--version", VERSION, SERIALWRAP_RELEASE_BASE_URL=srv.base)
    assert r.returncode != 0, _out(r)
    assert env.setup_args() is None
    assert env.tmpdir_empty()


def test_missing_release_asset_fails(env):
    with _ReleaseServer() as srv:
        r = env.run("--version", "0.3.2", SERIALWRAP_RELEASE_BASE_URL=srv.base)
    assert r.returncode != 0, _out(r)
    assert env.setup_args() is None
    assert env.tmpdir_empty()


# ---------- 系統需求 ----------

@pytest.mark.parametrize("override,needle", [
    ({"SERIALWRAP_INSTALL_ARCH": "aarch64"}, "x86_64"),
    ({"SERIALWRAP_INSTALL_GLIBC": "2.35"}, "2.39"),
    ({"SERIALWRAP_INSTALL_GLIBC": "2.4"}, "2.39"),
    ({"SERIALWRAP_INSTALL_GLIBC": "unknown"}, "glibc"),
    ({"SERIALWRAP_INSTALL_PYTHON312": "/nonexistent/python3.12"}, "python3.12"),
])
def test_requirement_unmet_rc2_without_setup(env, tmp_path, override, needle):
    bundle = _make_bundle(tmp_path / "bundle")
    with _ReleaseServer() as srv:
        r_bundle = env.run("--bundle", str(bundle), **override)
        r_download = env.run("--version", VERSION, SERIALWRAP_RELEASE_BASE_URL=srv.base, **override)
    for r in (r_bundle, r_download):
        assert r.returncode == 2, _out(r)
        assert needle in r.stderr
        assert "requirement" in r.stderr.lower()
    assert env.setup_args() is None
    assert srv.requests == [], "需求不符時不得開始下載"
    assert env.tmpdir_empty()


def test_newer_glibc_accepted(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    for glibc in ("2.39", "2.40", "2.100", "3.0"):
        env.setup_log.unlink(missing_ok=True)
        r = env.run("--bundle", str(bundle), SERIALWRAP_INSTALL_GLIBC=glibc)
        assert r.returncode == 0, glibc + _out(r)
        assert env.setup_args() == ["setup"]


# ---------- 舊 pipx 安裝提示 ----------

def test_pipx_list_hint_without_auto_uninstall(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    r = env.run("--bundle", str(bundle), FAKE_PIPX_LIST="serialwrap 0.3.2\nother 1.0\n")
    assert r.returncode == 0, _out(r)
    assert "pipx uninstall serialwrap" in _out(r)
    calls = env.pipx_log.read_text(encoding="utf-8").splitlines()
    assert calls and all(c.startswith("list") for c in calls), calls


def test_pipx_venv_dir_hint(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    venv = env.home / ".local" / "pipx" / "venvs" / "serialwrap"
    venv.mkdir(parents=True)
    r = env.run("--bundle", str(bundle))
    assert r.returncode == 0, _out(r)
    assert "pipx uninstall serialwrap" in _out(r)
    assert venv.is_dir()
    calls = env.pipx_log.read_text(encoding="utf-8").splitlines() if env.pipx_log.exists() else []
    assert not any("uninstall" in c for c in calls), calls


def test_no_pipx_hint_without_legacy_install(env, tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    r = env.run("--bundle", str(bundle), FAKE_PIPX_LIST="serialwrap-other 1.0\n")
    assert r.returncode == 0, _out(r)
    assert "pipx uninstall" not in _out(r)
