"""systemd 容器 holder 掃描的 fail-closed 單元測試（G7，sol 300f383 Medium）。

`_PROC_FD_SCAN` 原本只要 fd 目錄可列舉就把行程計為 readable，之後 readlink 的 PermissionError 被吞掉；
兩輪掃描都無法解析的現存行程仍被視為已覆蓋，`_holder_pids()` 可能回傳空集合（假釋放）。

本檔以暫存目錄模擬 /proc 樹，對 run_systemd_container._PROC_FD_SCAN 的原始碼（exec 載入，不另行複製）
注入 readlink 錯誤（PermissionError／FileNotFoundError），並以 monkeypatch 取代兩輪掃描，驗證 `_holder_pids()`。
全部為行程內測試：不啟動子行程、不開 socket、不開 PTY、不碰裝置。
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_RSC = None


def _rsc():
    """載入 scripts.docker.run_systemd_container（寫法同 tests/test_docker_l15.py）。"""
    global _RSC
    if _RSC is None:
        sys.path.insert(0, str(REPO / "tools" / "uart_stdio_bridge" / "src"))
        sys.path.insert(0, str(REPO))
        try:
            from scripts.docker import run_systemd_container as rsc  # noqa: PLC0415
        finally:
            sys.path.remove(str(REPO))
            sys.path.remove(str(REPO / "tools" / "uart_stdio_bridge" / "src"))
        _RSC = rsc
    return _RSC


def _run_scan(proc_root: Path, device: Path, readlink) -> dict:
    """以與子行程相同的原始碼執行 scan()，target 取法同 CLI（realpath）。"""
    namespace: dict = {"__name__": "_holder_scan_probe"}
    exec(compile(_rsc()._PROC_FD_SCAN, "<_PROC_FD_SCAN>", "exec"), namespace)
    return namespace["scan"](str(proc_root), os.path.realpath(device), readlink=readlink)


def _device(tmp_path: Path) -> Path:
    dev = tmp_path / "usb-fake-0-pty"
    dev.write_text("", encoding="utf-8")
    return dev


def _proc_tree(root: Path, procs: dict[int, list[Path]], *, no_fd_dirs: tuple[int, ...] = ()) -> Path:
    """建立假 /proc：procs 的每個 fd 為指向目標的符號連結；no_fd_dirs 模擬無 fd 目錄的行程（已結束或無 fd 表）。"""
    root.mkdir(parents=True, exist_ok=True)
    for pid, targets in procs.items():
        fd_dir = root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for number, target in enumerate(targets, start=3):
            (fd_dir / str(number)).symlink_to(target)
    for pid in no_fd_dirs:
        (root / str(pid)).mkdir(parents=True)
    (root / "cpuinfo").write_text("", encoding="utf-8")  # 非數字項目應被略過
    return root


def _readlink_faults(faults: dict[tuple[int, str], BaseException]):
    """回傳 readlink 替身：(pid, fd 編號) 命中時拋出指定例外，其餘轉交真實 os.readlink。"""
    def _readlink(path):
        key = (int(Path(path).parts[-3]), Path(path).name)
        if key in faults:
            raise faults[key]
        return os.readlink(path)
    return _readlink


def _fake_scan_proc_fds(root: Path, faults_by_identity: dict[str, dict[tuple[int, str], BaseException]]):
    """取代 _scan_proc_fds：root 與 daemon 使用者各自套用其權限錯誤表（as_user=None 為 root）。"""
    def _scan(target, *, as_user):
        identity = "root" if as_user is None else as_user
        return _run_scan(root, Path(target), _readlink_faults(faults_by_identity.get(identity, {})))

    return _scan


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_unreadable_fd_is_not_counted_as_readable(tmp_path):
    """G7 回歸：fd 目錄可列舉、但 readlink 拋 PermissionError 的現存行程不得計為 readable（已覆蓋）。"""
    dev = _device(tmp_path)
    root = _proc_tree(tmp_path / "proc", {200: [dev]})
    out = _run_scan(root, dev, _readlink_faults({(200, "3"): PermissionError(errno.EACCES, "denied")}))
    assert 200 not in out["readable"], out
    assert out["unresolved"] == [200], out
    assert out["unresolved_details"] == [
        {"pid": 200, "fd": "3", "stage": "readlink", "errno": errno.EACCES, "error": "PermissionError"},
    ], out
    assert out["holders"] == [], out


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_is_found_despite_unreadable_sibling_fd(tmp_path):
    """同一行程有一個 fd 無法解析、另一個 fd 指向裝置：仍判定為 holder。"""
    dev = _device(tmp_path)
    other = tmp_path / "other-file"
    other.write_text("", encoding="utf-8")
    root = _proc_tree(tmp_path / "proc", {300: [other, dev]})
    out = _run_scan(root, dev, _readlink_faults({(300, "3"): PermissionError(errno.EACCES, "denied")}))
    assert out["holders"] == [300], out


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_vanished_fd_and_process_are_ignored(tmp_path):
    """fd 於讀取期間關閉（readlink FileNotFoundError）與無 fd 目錄的行程皆可忽略：不計為 unresolved，亦不偽造持有者。"""
    dev = _device(tmp_path)
    other = tmp_path / "other-file"
    other.write_text("", encoding="utf-8")
    root = _proc_tree(tmp_path / "proc", {400: [dev], 401: [other]}, no_fd_dirs=(402,))
    out = _run_scan(root, dev, _readlink_faults({(400, "3"): FileNotFoundError(errno.ENOENT, "closed")}))
    assert out["unresolved"] == [], out
    assert out["holders"] == [], out
    assert {401, 402} <= set(out["readable"]), out


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_pids_fail_closed_when_both_identities_cannot_resolve(tmp_path, monkeypatch):
    """G7 回歸：root 與 swtest 都無法解析現存行程 700 的 fd 表時須 fail-closed，不得回傳空集合（假釋放）。"""
    rsc = _rsc()
    dev = _device(tmp_path)
    root = _proc_tree(tmp_path / "proc", {700: [dev]})
    denied = {(700, "3"): PermissionError(errno.EACCES, "denied")}
    monkeypatch.setattr(rsc, "_scan_proc_fds", _fake_scan_proc_fds(root, {"root": denied, rsc.systemd_container.USER: denied}))
    monkeypatch.setattr(rsc, "_proc_uid", lambda pid: {700: 1000}.get(pid))
    with pytest.raises(RuntimeError, match="700"):
        rsc._holder_pids(dev)


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_pids_accepts_other_identity_resolution(tmp_path, monkeypatch):
    """root 無法解析行程 700、但 swtest 可完整解析：以 swtest 的結果為準，回報真實持有者 [700]。"""
    rsc = _rsc()
    dev = _device(tmp_path)
    root = _proc_tree(tmp_path / "proc", {700: [dev]})
    monkeypatch.setattr(rsc, "_scan_proc_fds", _fake_scan_proc_fds(
        root, {"root": {(700, "3"): PermissionError(errno.EACCES, "denied")}}))
    monkeypatch.setattr(rsc, "_proc_uid", lambda pid: {700: 1000}.get(pid))
    assert rsc._holder_pids(dev) == [700]


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_pids_empty_only_when_some_identity_fully_resolved(tmp_path, monkeypatch):
    """無人持有時：行程 700 的 fd 表至少由一個身分完整解析，才可回傳空集合（非假釋放）。"""
    rsc = _rsc()
    dev = _device(tmp_path)
    other = tmp_path / "other-file"
    other.write_text("", encoding="utf-8")
    root = _proc_tree(tmp_path / "proc", {700: [other]})
    denied = {(700, "3"): PermissionError(errno.EACCES, "denied")}
    monkeypatch.setattr(rsc, "_scan_proc_fds", _fake_scan_proc_fds(root, {"root": denied}))
    monkeypatch.setattr(rsc, "_proc_uid", lambda pid: {700: 1000}.get(pid))
    assert rsc._holder_pids(dev) == []


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_pids_ignores_process_started_between_scans(tmp_path, monkeypatch):
    """兩輪掃描之間新出現的 root 行程（例如 runuser 父行程）不在 root 掃描清單內，swtest 無法讀取亦不得 fail-closed。

    已知邊界：兩輪之間新出現且持有 PTY 的 root 行程不在判定範圍內，此為既有行為的殘留風險（見 _holder_pids 註解）。
    """
    rsc = _rsc()
    dev = _device(tmp_path)
    root_scan = {"pids": [100], "readable": [100], "holders": [], "unresolved": []}
    user_scan = {"pids": [100, 800], "readable": [100], "holders": [], "unresolved": [800]}
    monkeypatch.setattr(rsc, "_scan_proc_fds",
                        lambda target, *, as_user: root_scan if as_user is None else user_scan)
    monkeypatch.setattr(rsc, "_proc_uid", lambda pid: {100: 0, 800: 0}.get(pid))
    assert rsc._holder_pids(dev) == []


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_holder_pids_ignores_vanished_process(tmp_path, monkeypatch):
    """行程於讀取期間消失（uid 解析為 None、無 fd 目錄）時不得 fail-closed。"""
    rsc = _rsc()
    dev = _device(tmp_path)
    root = _proc_tree(tmp_path / "proc", {}, no_fd_dirs=(800,))
    monkeypatch.setattr(rsc, "_scan_proc_fds", _fake_scan_proc_fds(root, {}))
    monkeypatch.setattr(rsc, "_proc_uid", lambda pid: None)
    assert rsc._holder_pids(dev) == []
