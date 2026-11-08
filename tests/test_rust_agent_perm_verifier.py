"""P4-T15：`scripts/rust/verify_agent_perms.py`（§10.3 權限阻斷驗證工具）的紅燈測試。

計畫來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 15。

TDD 紅燈階段：驗證腳本尚未實作，所有測試以 `xfail(strict=True, reason="P4-T15 紅燈")` 標示；
`python3 -m pytest -q <本檔> --runxfail` 可看到真實失敗原因（腳本不存在、輸出不符）。

測試原則（與腳本的安全約束一致）：
- 只使用 `tmp_path` 下的檔案與目錄；不對真實安裝路徑寫入。
- 唯讀／擁有者情境以 chmod 建構，finally 一律還原權限，避免 tmp 清理失敗。
- 純函式（classify、posix_restore_checks、windows_privilege_possible）以注入依賴測試，不碰檔案系統。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rust" / "verify_agent_perms.py"


def _load():
    """延遲載入驗證腳本為模組；腳本不存在時在此以 FileNotFoundError 失敗。"""
    spec = importlib.util.spec_from_file_location("verify_agent_perms", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*targets):
    p = subprocess.run([sys.executable, str(SCRIPT), "--targets", *map(str, targets)], capture_output=True, text=True)
    return p, json.loads(p.stdout)


def _foreign_readonly_chain(p: Path) -> bool:
    euid = os.geteuid()
    real = Path(os.path.realpath(p))
    for q in [p, *p.parents, real, *real.parents]:
        if os.lstat(q).st_uid == euid:
            return False
        if q != q.parent and os.access(q.parent, os.W_OK):
            return False
    return True


def _cap_eff_zero() -> bool:
    try:
        line = next(l for l in Path("/proc/self/status").read_text().splitlines() if l.startswith("CapEff:"))
    except (OSError, StopIteration):
        return False
    return int(line.split()[1], 16) == 0


# ---- 需要實際檔案系統與子行程的整合測試 ----


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="POSIX 權限位元")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 會繞過權限位元")
def test_same_owner_readonly_tree_is_not_blocked(tmp_path):
    # R4-F3 反例（取代舊 test_readonly_tree_reports_blocked）：同一身分擁有的 0555 樹，
    # 此刻 write／rename／replace／parent_dir_write 全被拒，但擁有者可 chmod 恢復寫權（本測試的 finally 就是這麼做），
    # 因此不得判為有效阻斷。
    vdir = tmp_path / "versions" / "0.4.0"
    vdir.mkdir(parents=True)
    exe = vdir / "serialwrap"
    exe.write_bytes(b"x")
    exe.chmod(0o555)
    vdir.chmod(0o555)
    (tmp_path / "versions").chmod(0o555)
    try:
        p, rep = _run(exe, vdir)
        assert p.returncode == 3
        assert rep["verdict"] == "not_blocked" and rep["blocked"] is False
        assert {r["op"] for r in rep["results"]} >= {"write", "rename", "replace", "parent_dir_write"}
        assert not any(r["allowed"] for r in rep["results"])          # 當下操作全被拒 ≠ 有效阻斷
        owned = {c["path"] for c in rep["restore_checks"] if c["kind"] == "owner_chmod" and c["possible"] is True}
        assert {str(exe), str(vdir), str(vdir.parent)} <= owned
        assert exe.read_bytes() == b"x"
        assert stat.S_IMODE(exe.stat().st_mode) == 0o555             # 驗證器本身不得 chmod
        assert stat.S_IMODE(vdir.stat().st_mode) == 0o555
    finally:
        (tmp_path / "versions").chmod(0o755); vdir.chmod(0o755); exe.chmod(0o755)


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="依賴 /proc/self/status CapEff")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 會繞過權限位元")
def test_foreign_owned_readonly_target_reports_blocked():
    # 不同 owner 的 fixture：系統 sh（root 擁有、整條祖先鏈對本身分唯讀）
    target = Path(shutil.which("sh") or "/bin/sh")
    if not (_foreign_readonly_chain(target) and _cap_eff_zero()):
        pytest.skip("前提不成立：sh 的路徑鏈不是全由他人擁有且唯讀，或行程具 capability")
    before = (os.lstat(target).st_ino, os.lstat(target).st_mtime_ns)
    p, rep = _run(target)
    assert p.returncode == 0 and rep["verdict"] == "blocked" and rep["blocked"] is True
    assert rep["results"] and not any(r["allowed"] for r in rep["results"])
    assert rep["restore_checks"] and all(c["possible"] is False for c in rep["restore_checks"])
    assert {"owner_chmod", "ancestor_replace", "privilege"} <= {c["kind"] for c in rep["restore_checks"]}
    assert (os.lstat(target).st_ino, os.lstat(target).st_mtime_ns) == before


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_writable_tree_reports_not_blocked_and_restores(tmp_path):
    f = tmp_path / "serialwrap"
    f.write_bytes(b"orig")
    p, rep = _run(f)
    assert p.returncode == 3 and rep["blocked"] is False and rep["verdict"] == "not_blocked"
    assert f.read_bytes() == b"orig"
    assert not list(tmp_path.glob("*.permprobe*"))


# ---- 純函式（不碰檔案系統）：classify 與 POSIX 恢復能力檢查 ----

DENIED = [{"target": "/opt/sw/serialwrap", "op": "write", "allowed": False, "error": "EACCES"}]


def _chk(v):
    return [{"target": "t", "path": "t", "kind": "owner_chmod", "possible": v, "detail": None}]


def _fake(fs, writable, *, euid=1000, cap_eff=0, realpath=lambda p: p, fail=()):
    def lstat(p):
        if p in fail:
            raise PermissionError(p)
        uid, mode = fs[p]
        return SimpleNamespace(st_uid=uid, st_mode=mode)
    return dict(lstat=lstat, access=lambda p, mode: (p in writable) if mode & os.W_OK else True,
                realpath=realpath, euid=euid, cap_eff=cap_eff)


ROOT_RO = {"/": (0, 0o40755), "/opt/sw": (0, 0o40555), "/opt/sw/serialwrap": (0, 0o100555)}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_classify_requires_every_restore_check_false():
    m = _load()
    assert m.classify(DENIED, _chk(False)) == ("blocked", True, 0)
    assert m.classify(DENIED, _chk(True)) == ("not_blocked", False, 3)
    assert m.classify(DENIED, _chk(None)) == ("unknown", None, 4)
    assert m.classify([dict(DENIED[0], allowed=True)], _chk(None)) == ("not_blocked", False, 3)
    assert m.classify(DENIED, []) == ("unknown", None, 4)      # 沒做恢復能力檢查 → 證據不足


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_writable_grandparent_reports_ancestor_replace():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o40777)})               # 只有祖父目錄 /opt 可寫、無 sticky
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, {"/opt"}))
    assert [c["path"] for c in checks if c["kind"] == "ancestor_replace" and c["possible"] is True] == ["/opt/sw"]
    assert m.classify(DENIED, checks)[0] == "not_blocked"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_sticky_grandparent_with_foreign_child_is_blocked():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o41777)})               # sticky 且 /opt/sw 非本身分擁有 → 不能改名
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, {"/opt"}))
    assert checks and all(c["possible"] is False for c in checks)
    assert m.classify(DENIED, checks) == ("blocked", True, 0)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_agent_owned_ancestor_reports_owner_chmod():
    m = _load()
    fs = {"/": (0, 0o40755), "/srv": (0, 0o40755), "/srv/agent": (1000, 0o40555),
          "/srv/agent/bin": (0, 0o40555), "/srv/agent/bin/serialwrap": (0, 0o100555)}
    checks = m.posix_restore_checks("/srv/agent/bin/serialwrap", **_fake(fs, set()))
    assert [c["path"] for c in checks if c["kind"] == "owner_chmod" and c["possible"] is True] == ["/srv/agent"]
    assert m.classify(DENIED, checks)[0] == "not_blocked"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cap_fowner_reports_privilege_and_missing_capeff_is_unknown():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o40755)})
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, set(), cap_eff=1 << 3))
    assert [c["possible"] for c in checks if c["kind"] == "privilege"] == [True]
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, set(), cap_eff=None))
    assert [c["possible"] for c in checks if c["kind"] == "privilege"] == [None]
    assert m.classify(DENIED, checks) == ("unknown", None, 4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_unreadable_ancestor_is_unknown_not_blocked():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o40755)})
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, set(), fail={"/opt"}))
    assert any(c["path"] == "/opt" and c["possible"] is None for c in checks)
    assert m.classify(DENIED, checks) == ("unknown", None, 4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_symlink_target_checks_both_chains():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o40755), "/usr": (0, 0o40755), "/usr/bin": (0, 0o40755),
                          "/usr/bin/serialwrap": (0, 0o120777)})
    checks = m.posix_restore_checks("/usr/bin/serialwrap",
                                    **_fake(fs, set(), realpath=lambda p: "/opt/sw/serialwrap" if p == "/usr/bin/serialwrap" else p))
    paths = {c["path"] for c in checks if c["kind"] == "owner_chmod"}
    assert {"/usr/bin/serialwrap", "/usr/bin", "/opt/sw/serialwrap", "/opt/sw", "/opt"} <= paths


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_privilege_table():
    m = _load()
    f = m.windows_privilege_possible
    assert f(set(), "absent") is False
    assert f({"SeTakeOwnershipPrivilege"}, "absent") is True
    assert f({"SeRestorePrivilege"}, "absent") is True
    assert f({"SeBackupPrivilege"}, "absent") is True
    assert f(set(), "enabled") is True
    assert f(set(), "deny_only") is None                         # UAC 篩選權杖：證據不足


@pytest.mark.windows_native
@pytest.mark.process_rpc
@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL")
def test_windows_owner_with_denied_write_is_not_blocked(tmp_path):
    # Windows 版 R4-F3 反例：本身分擁有的檔案即使 DACL deny (W,D)，擁有者仍隱含 WRITE_DAC，可自行改回
    f = tmp_path / "serialwrap.exe"
    f.write_bytes(b"x")
    api = _load().WindowsNativeApi()
    token_sid, _privileges, _admin = api.query_process_token()
    assert token_sid is not None, "無法取得目前 TokenUser SID，不能建立 owner 測試夾具"
    account = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    try:
        owner_sid, _owner_rights, owner_query_ok = api.query_owner_and_dacl(str(f))
        if owner_query_ok and owner_sid != token_sid:
            # 提權 token 建檔時，Windows 預設 owner 可能是 Administrators 群組；明確改為 TokenUser。
            subprocess.run(["icacls", str(f), "/setowner", account], check=True, capture_output=True)
        # 隔離掉 tmp_path 繼承的 OWNER RIGHTS ACE，讓測試只驗證 TokenUser 的隱含 WRITE_DAC。
        subprocess.run(["icacls", str(f), "/inheritance:r"], check=True, capture_output=True)
        subprocess.run(["icacls", str(f), "/grant", f"{account}:(RX)"], check=True, capture_output=True)
        subprocess.run(["icacls", str(f), "/deny", f"{account}:(W,D)"], check=True, capture_output=True)
        p, rep = _run(f)
        assert p.returncode == 3 and rep["verdict"] == "not_blocked" and rep["blocked"] is False
        assert [r["allowed"] for r in rep["results"] if r["target"] == str(f) and r["op"] == "write"] == [False]
        target_checks = [c for c in rep["restore_checks"] if c["path"] == str(f)]
        owner_sid, owner_rights_ace, owner_query_ok = api.query_owner_and_dacl(str(f))
        assert owner_query_ok and owner_sid == token_sid and not owner_rights_ace
        target_diagnostic = {
            "owner_matches_token": token_sid is not None and owner_sid == token_sid,
            "owner_rights_ace": owner_rights_ace,
            "owner_query_ok": owner_query_ok,
            "write_dac": api.test_open(str(f), 0x00040000),
            "write_owner": api.test_open(str(f), 0x00080000),
        }
        assert any(c["kind"] in ("owner", "dacl_write") and c["possible"] is True
                   for c in target_checks), {"target_checks": target_checks, "diagnostic": target_diagnostic}
        subprocess.run(["icacls", str(f), "/reset"], check=True, capture_output=True)
        assert f.read_bytes() == b"x"
    finally:
        subprocess.run(["icacls", str(f), "/reset"], check=False, capture_output=True)


# ---- CLI 介面：參數錯誤、--json-out ----


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_bad_args_exit2():
    # 腳本不存在時 Python 也會回 exit 2，因此先斷言檔案存在，並要求 stderr 明確指出缺少 --targets。
    assert SCRIPT.is_file(), "scripts/rust/verify_agent_perms.py 尚未交付"
    p = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode == 2
    assert "--targets" in p.stderr


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_json_out_file_written(tmp_path):
    target = tmp_path / "serialwrap"
    target.write_bytes(b"x")
    out = tmp_path / "report.json"
    p = subprocess.run([sys.executable, str(SCRIPT), "--targets", str(target), "--json-out", str(out)],
                       capture_output=True, text=True)
    assert out.read_text(encoding="utf-8") == p.stdout


# ---- 真實 target 不變性（只查詢、不改寫、不改權限、中斷無殘留） ----


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_verifier_never_touches_real_target(tmp_path):
    target = tmp_path / "serialwrap"
    target.write_bytes(b"orig")
    before = target.stat()
    _run(target)
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert target.read_bytes() == b"orig"
    assert not list(tmp_path.glob("*.permprobe*"))


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="POSIX 權限位元與 uid／gid")
def test_verifier_never_changes_permissions(tmp_path):
    # 0555 target 置於 tmp_path 下三層目錄；前後比對 target 與每個祖先目錄的 mode／uid／gid。
    l1 = tmp_path / "l1"
    l2 = l1 / "l2"
    l3 = l2 / "l3"
    l3.mkdir(parents=True)
    target = l3 / "serialwrap"
    target.write_bytes(b"x")
    chain = [target, l3, l2, l1, tmp_path]

    def snapshot():
        return [(p, s.st_mode, s.st_uid, s.st_gid) for p in chain for s in [os.lstat(p)]]

    target.chmod(0o555)
    for d in (l3, l2, l1):
        d.chmod(0o555)
    try:
        before = snapshot()
        _run(target)
        assert snapshot() == before
    finally:
        for d in (l1, l2, l3):
            d.chmod(0o755)
        target.chmod(0o755)


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="SIGKILL 僅 POSIX")
def test_verifier_interrupted_leaves_no_real_change(tmp_path):
    target = tmp_path / "serialwrap"
    target.write_bytes(b"orig")
    st0 = target.stat()
    # 模擬上次中斷殘留的舊 sentinel（mtime 設為 2 小時前，超過 1 小時門檻）
    stale = tmp_path / ".permprobe-99999-deadbeef"
    stale.write_bytes(b"")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    # 多個中斷時點：每次啟動後以 SIGKILL 終止，target 必須不變
    for delay in (0.0, 0.01, 0.03, 0.06):
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--targets", str(target)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(delay)
        proc.send_signal(signal.SIGKILL)
        proc.communicate()
        st = target.stat()
        assert (st.st_ino, st.st_mtime_ns, st.st_size) == (st0.st_ino, st0.st_mtime_ns, st0.st_size)
        assert target.read_bytes() == b"orig"
    # 再次執行應清掉超過 1 小時的舊 sentinel
    _run(target)
    assert not stale.exists()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_classify_empty_results_is_unknown():
    m = _load()
    assert m.classify([], _chk(False)) == ("unknown", None, 4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_classify_allowed_none_is_unknown():
    m = _load()
    unclassified_op = [{"target": "/opt/sw/serialwrap", "op": "write", "allowed": None, "error": "ETXTBSY"}]
    assert m.classify(unclassified_op, _chk(False)) == ("unknown", None, 4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cap_eff_nonzero_without_target_bits_is_unknown():
    m = _load()
    fs = dict(ROOT_RO, **{"/opt": (0, 0o40755)})
    # CAP_SYS_ADMIN is bit 21 (1 << 21), bits 0, 1, 3 are 0
    checks = m.posix_restore_checks("/opt/sw/serialwrap", **_fake(fs, set(), cap_eff=1 << 21))
    assert [c["possible"] for c in checks if c["kind"] == "privilege"] == [None]
    assert m.classify(DENIED, checks) == ("unknown", None, 4)


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="POSIX 檔案系統權限")
def test_directory_target_probes_inside_directory(tmp_path):
    # 可寫目錄 target 應判定 allowed: True
    dir_writable = tmp_path / "writable_dir"
    dir_writable.mkdir()
    p, rep = _run(dir_writable)
    write_op = next(r for r in rep["results"] if r["target"] == str(dir_writable) and r["op"] == "write")
    assert write_op["allowed"] is True
    assert rep["verdict"] == "not_blocked"

    # 唯讀目錄 target 應判定 allowed: False，且錯誤非 EISDIR
    dir_ro = tmp_path / "ro_dir"
    dir_ro.mkdir()
    dir_ro.chmod(0o555)
    try:
        p, rep = _run(dir_ro)
        write_op = next(r for r in rep["results"] if r["target"] == str(dir_ro) and r["op"] == "write")
        assert write_op["allowed"] is False
        assert write_op["error"] != "IsADirectoryError"
    finally:
        dir_ro.chmod(0o755)


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="POSIX 符號連結與檔案屬性")
def test_clean_stale_sentinels_does_not_delete_dirs_or_symlinks(tmp_path):
    m = _load()
    old = time.time() - 7200

    # 1. 建立以 .permprobe- 為前綴的目錄，內有檔案
    probe_dir = tmp_path / ".permprobe-12345-0123456789abcdef"
    probe_dir.mkdir()
    (probe_dir / "notes.txt").write_text("keep me")
    os.utime(probe_dir, (old, old))

    # 2. 建立以 .permprobe- 為前綴的符號連結
    target_file = tmp_path / "real_file.txt"
    target_file.write_text("target")
    probe_link = tmp_path / ".permprobe-12345-fedcba9876543210"
    probe_link.symlink_to(target_file)
    try:
        os.lutime(probe_link, (old, old))
    except (AttributeError, OSError):
        pass

    # 3. 建立正常的過期 sentinel 普通檔案
    stale_file = tmp_path / ".permprobe-12345-1122334455667788"
    stale_file.write_bytes(b"probe")
    os.utime(stale_file, (old, old))

    m._clean_stale_sentinels(str(tmp_path))

    # 目錄與符號連結必須完整保留，只有符合 regex 之普通檔案被刪除
    assert probe_dir.is_dir()
    assert (probe_dir / "notes.txt").exists()
    assert probe_link.is_symlink()
    assert not stale_file.exists()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_api_shim_failed_token_query_is_unknown():
    m = _load()

    class FailingTokenShim:
        def query_process_token(self):
            return None, None, None  # OpenProcessToken 失敗

        def query_owner_and_dacl(self, path):
            return "S-1-5-21-123", False, True

        def test_open(self, path, access, is_dir=False):
            return False  # ERROR_ACCESS_DENIED

    checks = m.windows_restore_checks("C:\\serialwrap", _win_api=FailingTokenShim())
    # token 查詢失敗時，owner 與 privilege 必須為 None（證據不足），classify 必須回 unknown
    assert any(c["kind"] == "owner" and c["possible"] is None for c in checks)
    assert any(c["kind"] == "privilege" and c["possible"] is None for c in checks)
    assert m.classify(DENIED, checks) == ("unknown", None, 4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_api_shim_access_denied_vs_sharing_violation():
    m = _load()

    class SharingViolationShim:
        def test_open(self, path, access, is_dir=False):
            # 模擬 ERROR_SHARING_VIOLATION (32) 非權限拒絕
            return None

    results = m.probe_target_operations("C:\\serialwrap\\serialwrap.exe", _win_api=SharingViolationShim())
    rename_op = next(r for r in results if r["op"] == "rename")
    assert rename_op["allowed"] is None  # 不是 False！


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_resolved_chain_checked():
    m = _load()

    class MockWinApi:
        def __init__(self):
            self.tested_paths = []

        def query_process_token(self):
            return "S-1-5-21-123", {"SeChangeNotifyPrivilege"}, "absent"

        def query_owner_and_dacl(self, path):
            self.tested_paths.append(path)
            return "S-1-5-21-999", False, True

        def test_open(self, path, access, is_dir=False):
            return False

    api = MockWinApi()
    checks = m.windows_restore_checks("C:\\sw\\serialwrap.exe", _win_api=api)
    assert any("C:\\sw" in c["path"] for c in checks)
