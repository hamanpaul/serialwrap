"""P8 Task 11（09-slice24-transfer-flash.md）：Windows 原生 COM 的外部工具裝置交接黑箱（OD-15 擁有者 host gate）。

凍結案例 `s24-ho-windows-com`：`test_windows_access_denied_holder_unknown`。需原生 Windows 與 com0com pair
`SW_TEST_COM_PAIR=COM20:COM21`（OD-08／OD-15），daemon 只放行 DUT 端 `COM20`（`SERIALWRAP_TEST_DEVICE_ALLOWLIST`），
其餘目錄由 `SwEnv` 隔離（OD-11）。非原生 Windows（含 WSL）由 `tests/conftest.py` 以 `windows_native` 標 SKIP；
無 pair 時以 `windows_com` 標 SKIP——兩者皆記「證據不足」，**不得**計為 PASS（WSL／記憶體替身不得當此案例證據）。

外部持有者以子行程 `CreateFileW(dwShareMode=0)` 獨占開 COM（模擬韌體升級工具）；Windows 無 `/proc`，holder 一律不明：
`DEVICE_STILL_HELD` 帶 `pids:[]`、`holder_unknown:true`（BC-04），不可視為可收回。
profile 為 `platform: passthrough`（explicit target attach 零 TX，SC-35）。stdlib＋pytest，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bb_harness import is_rust

pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com, pytest.mark.process_rpc]

_META_KEYS = ("id", "version", "code_fingerprint")
_DUT_COM = "COM20"

_PROFILE = f"""profiles:
  p8-win-passthrough:
    platform: passthrough
    ready_probe: ''
targets:
  - com: COM0
    profile: p8-win-passthrough
    device_by_id: {_DUT_COM}
"""

# 子行程：以 dwShareMode=0 獨占開 COM，回報 "OK" 或 "ERR <winerror>"；之後依 stdin 指令寫入（"W <hex>"）或讀到 EOF 即關閉。
_CHILD = r"""
import ctypes, sys
from ctypes import wintypes
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          wintypes.LPVOID]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
GENERIC_RW = 0x80000000 | 0x40000000
OPEN_EXISTING = 3
INVALID = wintypes.HANDLE(-1).value
h = k32.CreateFileW("\\\\.\\" + sys.argv[1], GENERIC_RW, 0, None, OPEN_EXISTING, 0, None)
if h == INVALID or h is None:
    sys.stdout.write("ERR %d\n" % ctypes.get_last_error()); sys.stdout.flush(); sys.exit(1)
sys.stdout.write("OK\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if line.startswith("W "):
        data = bytes.fromhex(line[2:])
        n = wintypes.DWORD(0)
        ok = k32.WriteFile(h, data, len(data), ctypes.byref(n), None)
        sys.stdout.write("WROTE %d %d\n" % (1 if ok else 0, n.value)); sys.stdout.flush()
k32.CloseHandle(h)
"""


class _ExclusiveComHolder:
    """單一子行程以 `CreateFileW(dwShareMode=0)` 獨占持有 COM；`stop()` 關 stdin（子行程關 handle 結束）。"""

    def __init__(self, com: str) -> None:
        self.com = com
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 10.0) -> None:
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _CHILD, self.com],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        if line != "OK":
            proc.kill()
            proc.wait(timeout=timeout_s)
            raise AssertionError(f"外部工具無法獨占開啟 {self.com}（{line!r}）")
        self._proc = proc

    def write(self, data: bytes) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(f"W {data.hex()}\n")
        proc.stdin.flush()
        reply = proc.stdout.readline().split()
        assert reply[:2] == ["WROTE", "1"] and int(reply[2]) == len(data), reply

    def stop(self, timeout_s: float = 10.0) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout_s)


def _body(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in _META_KEYS}


def _require_pair() -> None:
    pair = os.environ.get("SW_TEST_COM_PAIR")
    if os.name != "nt" or not pair:
        pytest.skip("BLOCKED-OWNER：需原生 Windows 與 SW_TEST_COM_PAIR（OD-08／OD-15；證據不足，非 PASS）")
    assert pair == "COM20:COM21", "本案例只允許 DUT-side pair COM20:COM21"


def _start_attached(sw_env) -> None:
    _require_pair()
    sw_env.prepare(targets={}, profile="passthrough")
    (Path(sw_env.profile_dir) / "p8.yaml").write_text(_PROFILE, encoding="utf-8")
    # SwEnv 已清除外層 SERIALWRAP_*；只放行受測端 COM20，避免掃描主機其他序列埠。
    sw_env._env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = _DUT_COM
    sw_env._start_daemon()
    sw_env.wait_state("COM0", "ATTACHED", timeout=20)


def _release(sw_env) -> dict:
    r = sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", "win handoff")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    assert r.json["session"]["state"] == "RELEASED", r.json
    return r.json


def _wal_max_seq(sw_env) -> int:
    return max((int(r.get("seq") or 0) for r in sw_env.wal_records()), default=0)


def test_windows_access_denied_holder_unknown(sw_env):
    """s24-ho-windows-com：外部程式獨占 COM 時 attach（含 --force）回 DEVICE_STILL_HELD、pids:[]、holder_unknown:true。"""
    _start_attached(sw_env)
    _release(sw_env)
    holder = _ExclusiveComHolder(_DUT_COM)
    holder.start()   # release 回成功前 daemon 已關閉 COM handle，外部獨占開啟必須成功
    try:
        for args in (("device", "attach", "--selector", "COM0"),
                     ("device", "attach", "--selector", "COM0", "--force")):
            r = sw_env.cli(*args)
            assert r.exit == 2, (args, r.exit, r.stdout, r.stderr)
            assert _body(r.json) == {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [],
                                     "holder_unknown": True, "selector": "COM0"}, args
            assert sw_env.session("COM0")["state"] == "RELEASED", "失敗不得清除 released 保護"
    finally:
        holder.stop()
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "ATTACHED", timeout=20)


def test_windows_selftest_released_holder_unknown(sw_env):
    _start_attached(sw_env)
    _release(sw_env)
    st = _body(sw_env.rpc("session.self_test", {"selector": "COM0"}))
    assert st["ok"] is True and st["classification"] == "RELEASED", st
    # BC-04（ab_participation: diff；parity-amendments #70）：Windows holder 一律不明 → 不可收回。
    # Python 基線 self-test 尚未跟上 BC-04（只看 pids），同 test_p7_selftest_recover 的先例。
    reclaimable = not is_rust()
    want_action = "device_attach" if reclaimable else "wait_external_flash"
    assert (st["external_holder"], st["reclaimable"], st["recommended_action"]) == ("none", reclaimable, want_action), st


def test_windows_external_flasher_cycle(sw_env):
    _start_attached(sw_env)
    _release(sw_env)
    seq_released = _wal_max_seq(sw_env)
    holder = _ExclusiveComHolder(_DUT_COM)
    holder.start()
    try:
        holder.write(b"\x55\x55FW-IMAGE\r\n")
        time.sleep(0.5)
    finally:
        holder.stop()
    seq_before_attach = _wal_max_seq(sw_env)
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "ATTACHED", timeout=20)
    gap_tx = [x for x in sw_env.wal_records()
              if x.get("dir") == "TX" and seq_released < int(x["seq"]) <= seq_before_attach]
    assert gap_tx == [], f"外部工具期間 WAL 無 daemon TX：{gap_tx}"
