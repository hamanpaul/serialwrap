#!/usr/bin/env python3
"""Agent 權限模型驗證工具（§10.3、Task 15）。

針對指定之受保護路徑驗證 Agent 執行身分是否已達成有效權限阻斷。
依安全防護要求（R4-F3）：
- 對真實目標路徑只做唯讀查詢與「開啟後立即關閉」探測，絕不呼叫 chmod、chown、setfacl、
  SetSecurityInfo 或 SetNamedSecurityInfoW。
- 絕不對真實目標執行 rename、replace、delete 或寫入資料。
- 目錄可寫性探測一律使用同目錄之 sentinel 檔案（.permprobe-<pid>-<rand>），並在 finally 中清理。
- 啟動時自動清理同目錄超過 1 小時之舊殘留 sentinel，且僅刪除符合格式之普通檔案，絕不遞迴刪除目錄。
- 執行恢復寫權能力檢查（restore_checks），若受測目標或其任何祖先目錄為 Agent 身分所擁有、
  或 Agent 具備修改 DACL／置換祖先目錄之特權，判定為 not_blocked（exit 3）。
- 任何證據不足（例如無法讀取 capability、查詢 API 失敗或祖先目錄無法查詢）一律判定為 unknown（exit 4），
  絕不判定為 blocked。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any

_SENTINEL_RE = re.compile(r"^\.permprobe-\d+-[0-9a-fA-F]{8,16}(\.r)?$")


def _ancestor_dirs(path: str) -> list[str]:
    """回傳指定路徑的所有祖先目錄（不含 path 本身），由近至遠，直到根目錄。"""
    ancestors = []
    curr = path
    while True:
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        ancestors.append(parent)
        curr = parent
    return ancestors


def _clean_stale_sentinels(dir_path: str, exclude_paths: set[str] | None = None) -> None:
    """清理目錄中超過 1 小時的舊 sentinel 檔案（.permprobe-<pid>-<rand>）。

    安全防護：
    - 絕不刪除 exclude_paths 所包含的路徑。
    - 僅刪除檔名符合 sentinel 格式且經 lstat 確認為普通檔案（S_ISREG）者。
    - 絕不呼叫 rmtree，絕不刪除目錄或符號連結。
    """
    try:
        if not os.path.isdir(dir_path):
            return
        now = time.time()
        for fname in os.listdir(dir_path):
            if not _SENTINEL_RE.match(fname):
                continue
            fpath = os.path.join(dir_path, fname)
            if exclude_paths and fpath in exclude_paths:
                continue
            try:
                st = os.lstat(fpath)
                if stat.S_ISREG(st.st_mode) and (now - st.st_mtime > 3600):
                    os.unlink(fpath)
            except OSError:
                pass
    except OSError:
        pass


def _run_sentinel_probe(dir_path: str) -> tuple[bool | None, str | None]:
    """在指定目錄內以 sentinel 檔案探測寫入／改名／置換／刪除能力。

    使用 O_CREAT | O_EXCL 與 O_NOFOLLOW 建立隨機檔名 sentinel，防止覆寫既有檔案或跟隨符號連結。
    若成功，回傳 (True, None)。
    若遇到權限拒絕（EACCES、EPERM、EROFS 或 Windows ERROR_ACCESS_DENIED），回傳 (False, error)。
    若遇到其他未預期作業系統錯誤（非權限問題），回傳 (None, error)。
    """
    pid = os.getpid()
    token = secrets.token_hex(8)
    sentinel = os.path.join(dir_path, f".permprobe-{pid}-{token}")
    sentinel_r = sentinel + ".r"

    allowed: bool | None = False
    err_str: str | None = None
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    try:
        # 1. create & write
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o600)
        try:
            os.write(fd, b"probe")
        finally:
            os.close(fd)

        # 2. append
        fd = os.open(sentinel, os.O_APPEND | os.O_WRONLY | nofollow)
        try:
            os.write(fd, b"+write")
        finally:
            os.close(fd)

        # 3. rename
        os.replace(sentinel, sentinel_r)

        # 4. replace
        fd = os.open(sentinel_r, os.O_WRONLY | os.O_TRUNC | nofollow)
        try:
            os.write(fd, b"replace")
        finally:
            os.close(fd)

        # 5. delete
        os.unlink(sentinel_r)
        allowed = True
    except OSError as exc:
        err_no = getattr(exc, "errno", None)
        win_err = getattr(exc, "winerror", None)
        is_denied = (
            err_no in (errno.EACCES, errno.EPERM, errno.EROFS)
            or win_err in (5,)  # ERROR_ACCESS_DENIED
        )
        if is_denied:
            allowed = False
            err_str = exc.__class__.__name__
        else:
            allowed = None
            err_str = exc.__class__.__name__
    finally:
        for s in (sentinel, sentinel_r):
            try:
                st = os.lstat(s)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(s)
            except OSError:
                pass

    return allowed, err_str


def probe_target_operations(target_path: str, *, _win_api: Any = None) -> list[dict[str, Any]]:
    """對單一 target 執行此刻操作權限探測。"""
    target_abs = os.path.abspath(target_path)
    parent_dir = os.path.dirname(target_abs)
    exclude = {target_abs}
    _clean_stale_sentinels(parent_dir, exclude_paths=exclude)

    is_dir = False
    try:
        st_t = os.lstat(target_abs)
        is_dir = stat.S_ISDIR(st_t.st_mode)
    except OSError:
        pass

    if is_dir:
        _clean_stale_sentinels(target_abs, exclude_paths=exclude)

    results: list[dict[str, Any]] = []

    # 1. write probe:
    # 若 target 為目錄：在該目錄內執行 sentinel probe（探測是否能在目錄內寫入／建立）。
    # 若 target 為普通檔案：以 O_APPEND 開啟並立即關閉，不寫入任何 byte，不 truncate。
    write_allowed: bool | None = False
    write_err: str | None = None

    if is_dir:
        write_allowed, write_err = _run_sentinel_probe(target_abs)
    else:
        try:
            flags = os.O_APPEND | os.O_WRONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
            fd = os.open(target_abs, flags)
            os.close(fd)
            write_allowed = True
            write_err = None
        except OSError as exc:
            err_no = getattr(exc, "errno", None)
            win_err = getattr(exc, "winerror", None)
            is_denied = (
                err_no in (errno.EACCES, errno.EPERM, errno.EROFS)
                or win_err in (5,)  # ERROR_ACCESS_DENIED
            )
            if is_denied:
                write_allowed = False
                write_err = exc.__class__.__name__
            else:
                write_allowed = None
                write_err = exc.__class__.__name__

    results.append({"target": target_abs, "op": "write", "allowed": write_allowed, "error": write_err})

    # 2. parent_dir_write probe using sentinel in parent directory
    parent_allowed, parent_err = _run_sentinel_probe(parent_dir)
    results.append({"target": target_abs, "op": "parent_dir_write", "allowed": parent_allowed, "error": parent_err})

    # 3. rename & delete determination
    rename_allowed: bool | None = False
    rename_err: str | None = None
    if sys.platform == "win32" or _win_api is not None:
        try:
            win_api = _win_api if _win_api is not None else _get_win_api()
            h = win_api.test_open(
                target_abs,
                access=0x00010000,  # DELETE
                is_dir=is_dir,
            )
            if h is True:
                rename_allowed = True
            elif h is False:
                rename_allowed = False
                rename_err = "ERROR_ACCESS_DENIED"
            else:
                rename_allowed = None
                rename_err = "WinError"
        except Exception as exc:
            rename_allowed = None
            rename_err = str(exc)
    else:
        try:
            st_parent = os.lstat(parent_dir)
            st_target = os.lstat(target_abs)
            parent_wx = os.access(parent_dir, os.W_OK | (os.X_OK if hasattr(os, "X_OK") else 0))
            is_sticky = bool(st_parent.st_mode & stat.S_ISVTX)
            euid = os.geteuid() if hasattr(os, "geteuid") else 0
            owned_by_us = (st_target.st_uid == euid)
            if parent_wx and (not is_sticky or owned_by_us):
                rename_allowed = True
            else:
                rename_allowed = False
                rename_err = "EACCES"
        except OSError as exc:
            err_no = getattr(exc, "errno", None)
            if err_no in (errno.EACCES, errno.EPERM, errno.EROFS):
                rename_allowed = False
                rename_err = exc.__class__.__name__
            else:
                rename_allowed = None
                rename_err = exc.__class__.__name__

    delete_allowed = rename_allowed
    delete_err = rename_err

    # 4. replace: rename allowed AND parent_dir_write allowed
    if rename_allowed is True and parent_allowed is True:
        replace_allowed: bool | None = True
        replace_err = None
    elif rename_allowed is False or parent_allowed is False:
        replace_allowed = False
        replace_err = rename_err or parent_err or "EACCES"
    else:
        replace_allowed = None
        replace_err = rename_err or parent_err or "EACCES"

    results.append({"target": target_abs, "op": "rename", "allowed": rename_allowed, "error": rename_err})
    results.append({"target": target_abs, "op": "delete", "allowed": delete_allowed, "error": delete_err})
    results.append({"target": target_abs, "op": "replace", "allowed": replace_allowed, "error": replace_err})

    return results


_SENTINEL = object()


def posix_restore_checks(
    target: str,
    *,
    lstat=os.lstat,
    access=os.access,
    realpath=os.path.realpath,
    euid: int | None = None,
    cap_eff: int | None = _SENTINEL,  # type: ignore
) -> list[dict[str, Any]]:
    """執行 POSIX 恢復寫權能力檢查。純函式，依賴可注入。"""
    target_abs = os.path.abspath(target)
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 0

    if cap_eff is _SENTINEL:
        if sys.platform.startswith("linux"):
            try:
                line = next(
                    l for l in Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
                    if l.startswith("CapEff:")
                )
                cap_eff = int(line.split()[1], 16)
            except Exception:
                cap_eff = None
        else:
            cap_eff = None

    checks: list[dict[str, Any]] = []

    literal_target = target_abs
    resolved_target = realpath(target)

    # 1. 字面鏈與解析鏈的所有唯一路徑：owner_chmod
    all_paths: list[str] = []
    seen: set[str] = set()
    for p in ([literal_target] + _ancestor_dirs(literal_target) + [resolved_target] + _ancestor_dirs(resolved_target)):
        if p not in seen:
            seen.add(p)
            all_paths.append(p)

    for p in all_paths:
        try:
            st = lstat(p)
            possible = (st.st_uid == euid)
            detail = None
        except Exception as exc:
            possible = None
            detail = str(exc)
        checks.append({
            "target": target_abs,
            "path": p,
            "kind": "owner_chmod",
            "possible": possible,
            "detail": detail,
        })

    # 2. 所有非根祖先目錄：ancestor_replace
    non_root_ancestors: list[str] = []
    seen_anc: set[str] = set()
    for a in (_ancestor_dirs(literal_target) + _ancestor_dirs(resolved_target)):
        if a != "/" and a not in seen_anc:
            seen_anc.add(a)
            non_root_ancestors.append(a)

    for a in non_root_ancestors:
        parent = os.path.dirname(a)
        try:
            parent_ok = access(parent, os.W_OK | (os.X_OK if hasattr(os, "X_OK") else 0))
            st_parent = lstat(parent)
            st_a = lstat(a)
            is_sticky = bool(st_parent.st_mode & stat.S_ISVTX)
            owned_by_us = (st_a.st_uid == euid)
            if parent_ok and (not is_sticky or owned_by_us):
                possible = True
                detail = None
            else:
                possible = False
                detail = None
        except Exception as exc:
            possible = None
            detail = str(exc)
        checks.append({
            "target": target_abs,
            "path": a,
            "kind": "ancestor_replace",
            "possible": possible,
            "detail": detail,
        })

    # 3. 特權檢查：privilege
    if euid == 0:
        priv_possible: bool | None = True
        priv_detail: str | None = "euid == 0"
    elif cap_eff is not None:
        if cap_eff == 0:
            priv_possible = False
            priv_detail = "CapEff == 0"
        elif bool(cap_eff & 0b1011):  # CAP_CHOWN (0), CAP_DAC_OVERRIDE (1), CAP_FOWNER (3) -> 1 | 2 | 8 = 11
            priv_possible = True
            priv_detail = f"CapEff={hex(cap_eff)} has CAP_CHOWN/CAP_DAC_OVERRIDE/CAP_FOWNER"
        else:
            # 非零 CapEff 但未含 bits 0, 1, 3：可能具備 CAP_SYS_ADMIN 等恢復能力，視為證據不足
            priv_possible = None
            priv_detail = f"CapEff={hex(cap_eff)} non-zero without bits 0,1,3 (other capabilities present)"
    else:
        priv_possible = None
        priv_detail = "CapEff not available and euid != 0"

    checks.append({
        "target": target_abs,
        "path": "<process>",
        "kind": "privilege",
        "possible": priv_possible,
        "detail": priv_detail,
    })

    return checks


def windows_privilege_possible(privileges: set[str], admin: str) -> bool | None:
    """判定 Windows token privilege 是否具備恢復寫權之能力。純函式。"""
    if "SeTakeOwnershipPrivilege" in privileges or "SeRestorePrivilege" in privileges or "SeBackupPrivilege" in privileges:
        return True
    if admin == "enabled":
        return True
    if admin == "deny_only":
        return None
    if admin == "absent":
        return False
    return None


class WindowsNativeApi:
    """Windows 原生 Win32 API 封裝，具備精確之 64-bit 型別簽章與 ctypes.Structure 解析。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        # 函式簽章綁定
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.GetCurrentProcess.argtypes = []

        self.kernel32.OpenProcessToken.restype = wintypes.BOOL
        self.kernel32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]

        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        self.kernel32.LocalFree.restype = ctypes.c_void_p
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]

        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]

        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

        self.advapi32.LookupPrivilegeNameW.restype = wintypes.BOOL
        self.advapi32.LookupPrivilegeNameW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]

        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]

        self.advapi32.GetAclInformation.restype = wintypes.BOOL
        self.advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
        ]

        self.advapi32.GetAce.restype = wintypes.BOOL
        self.advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]

        self.INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def test_open(self, path: str, access: int, is_dir: bool = False) -> bool | None:
        """嘗試以指定存取權開啟檔案或目錄，開啟成功即關閉。"""
        FILE_SHARE_ALL = 1 | 2 | 4
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        h = self.kernel32.CreateFileW(
            path,
            access,
            FILE_SHARE_ALL,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if h != self.INVALID_HANDLE_VALUE:
            self.kernel32.CloseHandle(h)
            return True
        err = self.ctypes.get_last_error()
        if err == 5:  # ERROR_ACCESS_DENIED
            return False
        return None

    def query_process_token(self) -> tuple[str | None, set[str] | None, str | None]:
        """查詢目前行程權杖之 User SID、Privileges 與 BUILTIN\\Administrators 狀態。"""
        wintypes = self.wintypes
        ctypes = self.ctypes

        TOKEN_QUERY = 0x0008
        h_token = wintypes.HANDLE()
        if not self.kernel32.OpenProcessToken(self.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h_token)):
            return None, None, None

        token_user_sid_str: str | None = None
        privileges: set[str] | None = None
        admin_status: str | None = None

        try:
            # 1. TokenUser
            TokenUser = 1
            needed = wintypes.DWORD()
            self.advapi32.GetTokenInformation(h_token, TokenUser, None, 0, ctypes.byref(needed))
            if needed.value > 0:
                buf = ctypes.create_string_buffer(needed.value)
                if self.advapi32.GetTokenInformation(h_token, TokenUser, buf, needed.value, ctypes.byref(needed)):
                    user_sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                    p_str = wintypes.LPWSTR()
                    if self.advapi32.ConvertSidToStringSidW(user_sid, ctypes.byref(p_str)):
                        token_user_sid_str = p_str.value
                        self.kernel32.LocalFree(p_str)

            # 2. TokenPrivileges
            TokenPrivileges = 3
            needed = wintypes.DWORD()
            self.advapi32.GetTokenInformation(h_token, TokenPrivileges, None, 0, ctypes.byref(needed))
            if needed.value > 0:
                buf = ctypes.create_string_buffer(needed.value)
                if self.advapi32.GetTokenInformation(h_token, TokenPrivileges, buf, needed.value, ctypes.byref(needed)):
                    class LUID(ctypes.Structure):
                        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

                    class LUID_AND_ATTRIBUTES(ctypes.Structure):
                        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

                    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
                    privs: set[str] = set()
                    array_type = LUID_AND_ATTRIBUTES * count
                    items = array_type.from_buffer(buf, 4)
                    for item in items:
                        name_buf = ctypes.create_unicode_buffer(256)
                        name_len = wintypes.DWORD(256)
                        if self.advapi32.LookupPrivilegeNameW(None, ctypes.byref(item.Luid), name_buf, ctypes.byref(name_len)):
                            privs.add(name_buf.value)
                    privileges = privs

            # 3. TokenGroups
            TokenGroups = 2
            needed = wintypes.DWORD()
            self.advapi32.GetTokenInformation(h_token, TokenGroups, None, 0, ctypes.byref(needed))
            if needed.value > 0:
                buf = ctypes.create_string_buffer(needed.value)
                if self.advapi32.GetTokenInformation(h_token, TokenGroups, buf, needed.value, ctypes.byref(needed)):
                    class SID_AND_ATTRIBUTES(ctypes.Structure):
                        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

                    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
                    # 依 64-bit C struct 對齊，DWORD 後對齊至 pointer boundary
                    offset = ctypes.sizeof(ctypes.c_void_p)
                    array_type = SID_AND_ATTRIBUTES * count
                    items = array_type.from_buffer(buf, offset)
                    found_admin = False
                    for item in items:
                        p_str = wintypes.LPWSTR()
                        if self.advapi32.ConvertSidToStringSidW(item.Sid, ctypes.byref(p_str)):
                            sid_val = p_str.value
                            self.kernel32.LocalFree(p_str)
                            if sid_val == "S-1-5-32-544":  # BUILTIN\Administrators
                                found_admin = True
                                SE_GROUP_USE_FOR_DENY_ONLY = 0x00000010
                                SE_GROUP_ENABLED = 0x00000004
                                if item.Attributes & SE_GROUP_ENABLED:
                                    admin_status = "enabled"
                                elif item.Attributes & SE_GROUP_USE_FOR_DENY_ONLY:
                                    admin_status = "deny_only"
                                else:
                                    admin_status = "absent"
                    if not found_admin:
                        admin_status = "absent"
        finally:
            self.kernel32.CloseHandle(h_token)

        return token_user_sid_str, privileges, admin_status

    def query_owner_and_dacl(self, path: str) -> tuple[str | None, bool, bool | None]:
        """查詢檔案或目錄之 Owner SID、是否具 OWNER RIGHTS ACE，以及查詢是否成功。"""
        ctypes = self.ctypes
        wintypes = self.wintypes

        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004

        p_owner_sid = ctypes.c_void_p()
        p_dacl = ctypes.c_void_p()
        p_sd = ctypes.c_void_p()

        ret = self.advapi32.GetNamedSecurityInfoW(
            path,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(p_owner_sid),
            None,
            ctypes.byref(p_dacl),
            None,
            ctypes.byref(p_sd),
        )
        if ret != 0:
            return None, False, False

        owner_sid_str: str | None = None
        has_owner_rights = False

        try:
            p_str = wintypes.LPWSTR()
            if self.advapi32.ConvertSidToStringSidW(p_owner_sid, ctypes.byref(p_str)):
                owner_sid_str = p_str.value
                self.kernel32.LocalFree(p_str)

            if p_dacl:
                acl_size = (wintypes.DWORD * 3)()
                if self.advapi32.GetAclInformation(p_dacl, ctypes.byref(acl_size), ctypes.sizeof(acl_size), 2):
                    count = acl_size[0]
                    for i in range(count):
                        p_ace = ctypes.c_void_p()
                        if self.advapi32.GetAce(p_dacl, i, ctypes.byref(p_ace)):
                            ace_sid = ctypes.c_void_p(p_ace.value + 8)
                            p_ace_str = wintypes.LPWSTR()
                            if self.advapi32.ConvertSidToStringSidW(ace_sid, ctypes.byref(p_ace_str)):
                                if p_ace_str.value == "S-1-3-4":  # OWNER RIGHTS SID
                                    has_owner_rights = True
                                self.kernel32.LocalFree(p_ace_str)
        finally:
            self.kernel32.LocalFree(p_sd)

        return owner_sid_str, has_owner_rights, True


_win_api_singleton = None


def _get_win_api() -> WindowsNativeApi:
    global _win_api_singleton
    if _win_api_singleton is None:
        _win_api_singleton = WindowsNativeApi()
    return _win_api_singleton


def windows_restore_checks(target: str, *, _win_api: Any = None) -> list[dict[str, Any]]:
    """執行 Windows 恢復寫權能力檢查。支援注入 mock win_api。"""
    target_abs = os.path.abspath(target)
    checks: list[dict[str, Any]] = []

    if sys.platform != "win32" and _win_api is None:
        return checks

    win_api = _win_api if _win_api is not None else _get_win_api()

    # 1. 取得 process token user SID 與 privileges
    token_user_sid_str, privileges, admin_status = win_api.query_process_token()

    # 祖先路徑收集：檢查字面鏈與 realpath 解析鏈
    literal_target = target_abs
    try:
        resolved_target = os.path.realpath(target_abs)
    except Exception:
        resolved_target = literal_target

    all_paths: list[str] = []
    seen: set[str] = set()
    for p in ([literal_target] + _ancestor_dirs(literal_target) + [resolved_target] + _ancestor_dirs(resolved_target)):
        if p not in seen:
            seen.add(p)
            all_paths.append(p)

    WRITE_DAC = 0x00040000
    WRITE_OWNER = 0x00080000
    DELETE = 0x00010000
    FILE_ADD_SUBDIRECTORY = 0x0004

    for p in all_paths:
        owner_sid_str, has_owner_rights, query_ok = win_api.query_owner_and_dacl(p)

        owner_possible: bool | None = None
        if query_ok and token_user_sid_str is not None:
            if owner_sid_str == token_user_sid_str:
                if not has_owner_rights:
                    owner_possible = True
            else:
                owner_possible = False
        else:
            owner_possible = None

        # dacl_write 探測
        w_dac = win_api.test_open(p, WRITE_DAC)
        w_owner = win_api.test_open(p, WRITE_OWNER)
        if w_dac is True or w_owner is True:
            dacl_write_possible: bool | None = True
        elif w_dac is False and w_owner is False:
            dacl_write_possible = False
        else:
            dacl_write_possible = None

        if query_ok and has_owner_rights:
            owner_possible = dacl_write_possible

        checks.append({
            "target": target_abs,
            "path": p,
            "kind": "owner",
            "possible": owner_possible,
            "detail": None,
        })
        checks.append({
            "target": target_abs,
            "path": p,
            "kind": "dacl_write",
            "possible": dacl_write_possible,
            "detail": None,
        })

    # ancestor_replace check（檢查字面與解析之非磁碟根祖先目錄）
    non_root_ancestors: list[str] = []
    seen_anc: set[str] = set()
    for a in (_ancestor_dirs(literal_target) + _ancestor_dirs(resolved_target)):
        parent = os.path.dirname(a)
        if parent == a:
            continue
        if a not in seen_anc:
            seen_anc.add(a)
            non_root_ancestors.append(a)

    for a in non_root_ancestors:
        parent = os.path.dirname(a)
        del_a = win_api.test_open(a, DELETE)
        add_p = win_api.test_open(parent, FILE_ADD_SUBDIRECTORY)
        if del_a is True and add_p is True:
            anc_possible: bool | None = True
        elif del_a is False or add_p is False:
            anc_possible = False
        else:
            anc_possible = None
        checks.append({
            "target": target_abs,
            "path": a,
            "kind": "ancestor_replace",
            "possible": anc_possible,
            "detail": None,
        })

    # privilege
    if privileges is not None and admin_status is not None:
        priv_possible = windows_privilege_possible(privileges, admin_status)
    else:
        priv_possible = None

    checks.append({
        "target": target_abs,
        "path": "<process>",
        "kind": "privilege",
        "possible": priv_possible,
        "detail": None,
    })

    return checks


def classify(results: list[dict[str, Any]], restore_checks: list[dict[str, Any]]) -> tuple[str, bool | None, int]:
    """判定整體權限阻斷狀態。純函式。

    回傳：(verdict, blocked, exit_code)
      verdict: "blocked" | "not_blocked" | "unknown"
      blocked: True | False | None
      exit_code: 0 | 3 | 4
    """
    # 1. 任一 op allowed=True 或任一 restore_check possible=True -> not_blocked / exit 3
    if any(r.get("allowed") is True for r in results):
        return ("not_blocked", False, 3)

    if any(c.get("possible") is True for c in restore_checks):
        return ("not_blocked", False, 3)

    # 2. results 為空、restore_checks 為空或有任一 possible=None / allowed=None -> unknown / exit 4
    if not results or not restore_checks:
        return ("unknown", None, 4)

    if any(r.get("allowed") is None for r in results) or any(c.get("possible") is None for c in restore_checks):
        return ("unknown", None, 4)

    # 3. 全部 op 為 False 且全部 restore_checks 為 False -> blocked / exit 0
    if all(r.get("allowed") is False for r in results) and all(c.get("possible") is False for c in restore_checks):
        return ("blocked", True, 0)

    return ("unknown", None, 4)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="verify_agent_perms.py",
        description="驗證 Agent 執行身分對受保護路徑之權限阻斷狀態（§10.3）。",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        required=True,
        help="欲驗證之受保護檔案或目錄路徑清單",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="將報告 JSON 同步寫入此檔案路徑",
    )

    args = parser.parse_args(argv)

    if sys.platform == "win32":
        user_domain = os.environ.get("USERDOMAIN", "")
        username = os.environ.get("USERNAME", "unknown")
        identity = f"{user_domain}\\{username}" if user_domain else username
    else:
        try:
            import pwd
            identity = pwd.getpwuid(os.geteuid()).pw_name
        except Exception:
            identity = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"

    all_results: list[dict[str, Any]] = []
    all_restore_checks: list[dict[str, Any]] = []

    for target in args.targets:
        target_abs = os.path.abspath(target)
        all_results.extend(probe_target_operations(target_abs))

        if sys.platform == "win32":
            all_restore_checks.extend(windows_restore_checks(target_abs))
        else:
            all_restore_checks.extend(posix_restore_checks(target_abs))

    verdict, blocked, exit_code = classify(all_results, all_restore_checks)

    report = {
        "identity": identity,
        "platform": sys.platform,
        "results": all_results,
        "restore_checks": all_restore_checks,
        "verdict": verdict,
        "blocked": blocked,
    }

    out_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(out_text)
    sys.stdout.flush()

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text, encoding="utf-8")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
