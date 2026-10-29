from __future__ import annotations

import base64
import contextlib
import dataclasses
import inspect
import itertools
import json
import logging
import os
import re
import shlex
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Callable, Literal

from . import serial_port as serial_port_mod
from . import uart_io as uart_io_mod
from .alias_registry import AliasRegistry
from .auth import AuthResolution, resolve_session_auth
from .config import ProfileTemplate, SessionProfile, UartProfile
from .command_completion import ERROR_INVALID_COMPLETION_REGEX, valid_completion_regex
from .constants import (
    BG_CAPTURE_MAX_BYTES,
    BG_CAPTURE_MAX_COUNT,
    DRAIN_TIMEOUT_MS,
    ERROR_DRAIN_STALLED,
    BOOT_BANNER_PATTERNS,
    BOOT_BANNER_TAIL_CHARS,
    BOOT_QUIET_WINDOW_S,
    BOOTLOADER_RX_TAIL_BYTES,
    ERROR_AUTOBOOT_QUIET,
    ERROR_BOOTLOADER_STUCK,
    ERROR_CREDENTIALS_UNRESOLVED,
    ERROR_READY_UNCONFIRMED,
    ERROR_RX_FLOOD,
    ERROR_TRANSPORT_STALL,
    HUMAN_ACTIVE_WINDOW_S,
    INTERACTIVE_TOMBSTONE_MAX,
    LOG_DIR,
    MAX_RECOVERY_LEASE_S,
    PASSTHROUGH_READY_QUIET_S,
    READY_RECONFIRM_MAX_ATTEMPTS,
    READY_RECONFIRM_MAX_S,
    READY_RECONFIRM_RETRY_S,
    REPROBE_BACKOFF_S,
    REPROBE_MAX_ATTEMPTS,
    REPROBE_MAX_INTERVAL_S,
    REPROBE_RX_IDLE_S,
    RX_FLOOD_BYTES_PER_10S,
    STATE_PATH,
    UBOOT_FALLBACK_PROMPTS,
    _HUMAN_PEER_GRACE_S,
)
from .device_watcher import DeviceInfo
from .file_transfer import DEFAULT_CHUNK_SIZE, DEFAULT_ECHO_TIMEOUT_S
from .login_fsm import (
    LOGIN_FSM_DETAIL_ERRORS,
    detect_boot_banner,
    detect_template,
    ensure_ready,
    matches_login_or_password,
    probe_ready,
)
from .transport_stall import classify_probe_failure, transport_stall_hint
from .serial_port import SerialPort, TxResult
from .uart_io import (
    PreservedConsoles,
    TxIncompleteError,
    TxPermit,
    TxUnknownError,
    UARTBridge,
    _pty_available,
)
from .util import clean_text, now_iso
from .wal import WalWriter


def _login_tx_refused(exc: BaseException) -> bool:
    """login FSM 在提交 fence 遭 quiet 或較新 login run 拒絕時回復為等待狀態。"""
    if not isinstance(exc, TxIncompleteError):
        return False
    return (exc.result.error or "").removeprefix("revoked:") in {
        "quiet",
        "stale_login_gen",
    }


def _self_test_tx_refused(exc: BaseException) -> bool:
    """self-test nonce probe 遭 quiet 拒絕時沿用可重試 quiet 回應。"""
    if not isinstance(exc, TxIncompleteError):
        return False
    return (exc.result.error or "").removeprefix("revoked:") == "quiet"


_ATTACHED_CONSOLE_LEASE_TIMEOUT_S = 86400.0

# 檔案傳輸每段等待逾時的下限（#157）：未顯式帶 chunk_timeout_s 時沿用
# profile.timeout_s，但夾一個地板防止 profile 被設過低導致傳輸必逾時。
_MIN_FILE_CHUNK_TIMEOUT_S = 5.0   # push 每個 chunk 等待下限
_MIN_FILE_PULL_TIMEOUT_S = 30.0   # pull 整段讀取等待下限（維持原 30.0 行為基準）
# echo-ACK 單一 slice 等待下限（#161 實機調校）：同樣依 profile.timeout_s 推導、夾地板，
# 地板即 file_transfer.DEFAULT_ECHO_TIMEOUT_S（5.0）。#166 已確認 448/512 的實機症狀
# 是板端缺 base64 與 prpl 約 505 字元單行限制的下游結果，並非 timeout 根因。
_MIN_FILE_ECHO_TIMEOUT_S = DEFAULT_ECHO_TIMEOUT_S


def _replace_state_file(tmp_path: str, dst_path: str, *, retry: bool | None = None) -> None:
    """``os.replace`` 包裝（#133）：Windows 上對 ``PermissionError`` 短退避重試。

    Windows 的 ``MoveFileEx(REPLACE_EXISTING)`` 在目的檔被讀者（``_load_state``、
    測試、防毒）短暫持有 handle 時會 ``WinError 5``；POSIX ``rename()`` 無此限制，
    故 ``retry`` 預設依 ``os.name``（nt → 重試、其餘 → 裸 replace、行為不變）。
    重試耗盡上拋，讓持續性失敗（ACL 壞掉等）可見而非靜默吞掉。
    """
    if retry is None:
        retry = os.name == "nt"
    if not retry:
        os.replace(tmp_path, dst_path)
        return
    delay_s = 0.01
    for attempt in range(6):
        try:
            os.replace(tmp_path, dst_path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(delay_s)
            delay_s *= 2


def device_sort_key(by_id: str, by_path: str | None) -> tuple[str, str]:
    """COM rank 排序鍵（#100）：以 by-id 路徑字串為主、by-path 為輔。

    同款晶片（如 CH340）by-id 會相同——此時改用 by-path 區分，沿用 ``DeviceWatcher``
    「by-id 優先、同 real_path 去重」的既有語意。回傳 tuple 確保穩定且可比較；
    FTDI 等 by-id 皆唯一的情境下 by-path 可傳 None 不影響排序。

    註：目前 ``DeviceInfo`` 尚無 by-path 欄位，FTDI 情境呼叫端會以 None 傳入；
    by-path fallback 的完整資料來源待 ``DeviceWatcher`` 後續補欄位（TODO #100）。
    """
    return (by_id or "", by_path or "")


class StateLoadError(RuntimeError):
    """既有 state.json 讀取失敗（非 JSON 損毀，如 PermissionError／暫時性 I/O）時拋出。

    用以與「確認的 JSON 格式損毀」區分：後者可備份重建，前者必須 fail closed——若帶病啟動，
    __init__ 尾段的 _save_state() 會以空狀態覆寫磁碟檔，遺失 RELEASED（裝置交接），重啟後
    daemon 重新 attach 已交給 flasher／人類的 tty 形成 two-reader（#82 / Codex 必修）。
    daemon 啟動層（daemon.py）攔截本例外並拒絕啟動。
    """


def _matches_any_bootloader_prompt(
    rx_tail: str,
    patterns: "list[str] | tuple[str, ...]",
) -> "str | None":
    """rx_tail 的最後一個非空／非純空白行是否符合 patterns 中任一 regex。

    回傳第一個命中的 pattern 字串；無命中、空 rx_tail、空 patterns 均返回 None。
    invalid regex 不拋出例外，直接略過該 pattern。
    """
    if not rx_tail or not patterns:
        return None
    # 從尾端找最後一個非空白行
    lines = rx_tail.splitlines()
    last_line: str | None = None
    for line in reversed(lines):
        if line.strip():
            last_line = line
            break
    if last_line is None:
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, last_line) is not None:
                return pattern
        except re.error:
            # invalid regex → 略過
            continue
    return None


def _login_fsm_rx_tail_detail(bridge: Any) -> str | None:
    """login FSM 失敗碼的 rx tail 摘要（#174）：清除控制碼／ANSI 後截尾 300 字元。

    找不到 ``rx_tail`` 或讀取例外一律回 None（getattr 防禦，對不支援 rx_tail 的
    fake bridge 行為零變更）；rx tail 為空亦回 None，不寫入空字串 detail。
    """
    tail_fn = getattr(bridge, "rx_tail", None)
    if not callable(tail_fn):
        return None
    try:
        raw = tail_fn()
    except Exception:
        return None
    if not raw:
        return None
    cleaned = clean_text(raw)[-300:]
    return cleaned or None


def _is_reboot_command(command: str) -> bool:
    # PDF ST-06 使用的立即 SysRq reboot。限定完整單一命令，不猜測 shell 串接。
    if re.fullmatch(r"\s*echo\s+(?:b|'b'|\"b\")\s*>\s*/proc/sysrq-trigger\s*", command):
        return True
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    base = os.path.basename(tokens[0])
    if base == "reboot":
        return True
    if base == "systemctl" and len(tokens) >= 2 and tokens[1] == "reboot":
        return True
    if base != "shutdown":
        return False
    return any(token == "-r" or token == "--reboot" or token.startswith("-r") for token in tokens[1:])


def _passthrough_completion_pattern(
    profile: SessionProfile,
    completion_regex: str | None,
) -> str | None:
    """依序選擇 passthrough 命令完成條件；None 表示只能等 RX 靜默。"""
    if completion_regex is not None:
        return completion_regex
    return profile.prompt_regex if valid_completion_regex(profile.prompt_regex) else None


def passthrough_file_transfer_error(session: dict[str, Any]) -> dict[str, Any]:
    """建立拒絕 passthrough 檔案傳輸的穩定 RPC 回應。"""
    return {
        "ok": False,
        "error_code": "PROFILE_NOT_COMMAND_CAPABLE",
        "hint": "passthrough profile 不支援 file.push／file.pull；請使用互動 console。",
        "session": session,
    }


def _strip_wrapped_command_echo(lines: list[str], command: str) -> list[str]:
    """移除含 BusyBox 雙 CR 折行標記的命令回顯；``lines`` 已經 ``clean_text`` 並切行。

    shell 回顯打到終端寬度（serial console 常見 80 欄）時會在回顯中插入換行
    （BusyBox ash 為 ``\\r\\r\\n``，clean_text 後成為換行）。只接合不同輸出行，
    行內空白保持原樣，避免把未回顯的有效 stdout（如 ``cat file`` 輸出 ``catfile``）
    誤認為命令。bash readline 的單行 ``" \\r"`` 插入空白由呼叫端以原始控制字元
    單獨辨識。
    """
    if not command or len(lines) < 2:
        return lines
    joined = ""
    # 終端寬度扣掉 prompt 後每段仍至少約 20 字元，以此保守估計回顯最多佔幾行
    for n, line in enumerate(lines[: len(command) // 20 + 2], 1):
        joined += line
        if joined.endswith(command):
            prefix = joined[:-len(command)]
            if len(prefix) < len(lines[0]):
                return lines[n:]
    return lines


@dataclasses.dataclass
class BackgroundCapture:
    cmd_id: str
    session_id: str
    from_seq: int
    quiet_window_s: float
    created_at: str
    chunks: "deque[str]" = dataclasses.field(default_factory=deque)
    last_seq: int = 0
    status: str = "active"
    error_code: str | None = None
    last_activity_mono: float = dataclasses.field(default_factory=time.monotonic)
    total_bytes: int = 0          # chunks 在記憶體的累計大小（#81，增量維護避免每次 O(n) 加總）
    dropped_chunks: int = 0       # 因環形上限丟棄的最舊 chunk 數（供 result cursor 對齊）

    def add_chunk(self, chunk: str, max_bytes: int) -> None:
        """新增一段 RX chunk 並維持記憶體環形上限（#81）。

        超過 ``max_bytes`` 時丟最舊 chunk（保留尾端最新），``dropped_chunks`` 累計，
        使 ``get_background_result`` 的絕對 chunk cursor 仍可對齊；至少保留一段，
        避免單一超大 chunk 無限丟棄。
        """
        if not chunk:
            return
        self.chunks.append(chunk)
        self.total_bytes += len(chunk)
        # deque.popleft 為 O(1)；原 list.pop(0) 每丟一段需整體位移（O(n)），狂吐/高頻 RX 下會把
        # OOM 修正路徑變成 CPU 熱點（Copilot 審查）。改用 deque 後仍維持「丟最舊、保留尾端」語意。
        while self.total_bytes > max_bytes and len(self.chunks) > 1:
            oldest = self.chunks.popleft()
            self.total_bytes -= len(oldest)
            self.dropped_chunks += 1

    def maybe_finalize(self) -> None:
        if self.status == "active" and time.monotonic() - self.last_activity_mono >= self.quiet_window_s:
            self.status = "done"


@dataclasses.dataclass
class InteractiveLease:
    interactive_id: str
    session_id: str
    owner: str
    created_at: str
    timeout_s: float
    last_activity_at: float = dataclasses.field(default_factory=time.monotonic)
    status: str = "active"
    recovery_mode: bool = False
    # 內部 lifecycle flag：recovery lease 開啟前是否已 suspend 人類 console lease；不透出 RPC。
    suspended_human: bool = False
    # human lease peer-loss grace：首次觀測到 console peer 消失的 monotonic 時間；peer 回復清為 None。
    peer_lost_at: float | None = None

    def touch(self) -> None:
        self.last_activity_at = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.last_activity_at > self.timeout_s


@dataclasses.dataclass
class SessionCapture:
    capture_id: str
    session_id: str
    log_path: str
    started_at: str
    line_count: int = 0
    byte_count: int = 0
    status: str = "active"


@dataclasses.dataclass(frozen=True)
class _ForegroundOperation:
    """同一 session/bridge epoch 的內部操作身分。

    ``foreground_busy`` 是對外狀態欄位，單獨不足以安全清理：舊 bridge 的
    callback 可能在 re-register 後才進入 finally。此 token 僅在 manager lock
    內建立／比對，不公開成 RPC 或 public session schema。
    """

    session_id: str
    bridge: "UARTBridge"
    bridge_generation: int
    cmd_id: str | None = None
    kind: str = "command"
    token: object = dataclasses.field(default_factory=object, compare=False)


@dataclasses.dataclass
class _AttachOp:
    """同步取得 UART 獨占並交給 attach worker 的一次性操作身分。"""

    token: object
    session_id: str
    by_id: str
    bridge_generation: int
    real_path: str
    port: SerialPort | None = None
    cancelled: threading.Event = dataclasses.field(default_factory=threading.Event)
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    probe_bridge: "UARTBridge | None" = None
    release_request: tuple[str, str | None] | None = None
    release_deadline: float | None = None
    bind_requested: bool = False
    port_transferred: bool = False
    port_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


@dataclasses.dataclass
class _DrainJob:
    session_id: str
    bridge: UARTBridge
    epoch: int
    kind: str
    reason: str
    drop_consoles: bool
    aborted_cmd_id: str | None
    done: threading.Event
    deadline: float
    aborted_foreground: bool = False
    attach_started: threading.Event | None = None
    receipt: Any | None = None
    target_by_id: str | None = None
    source: str = "cli"
    release_reason: str | None = None
    closed_consoles: int = 0


@dataclasses.dataclass
class _PostCloseAction:
    """lock 外需要執行的 bridge 操作，由 _close_interactive_locked 回傳。

    呼叫者在釋放 _lock 後執行 execute()。
    """
    bridge: "UARTBridge | None" = None
    needs_resume: bool = False
    clear_owner_after_resume: bool = False
    should_execute: Callable[[], bool] | None = None
    on_complete: Callable[[], None] | None = None

    def execute(self) -> None:
        try:
            if self.bridge is None or not self.needs_resume:
                return
            if self.should_execute is not None and not self.should_execute():
                return
            try:
                self.bridge.resume_interactive()
            finally:
                if self.clear_owner_after_resume and (
                    self.should_execute is None or self.should_execute()
                ):
                    self.bridge.set_interactive_owner(None)
        finally:
            if self.on_complete is not None:
                self.on_complete()


@dataclasses.dataclass
class SessionRuntime:
    session_id: str
    profile: SessionProfile
    # last_error 已改為 property（見下方 _last_error；#150）：值變更時自動清
    # last_error_detail，一處根除 stale detail。建構子不再接受 last_error=
    # （全 repo 建構皆僅 session_id+profile，已 grep 驗證）。
    detached_at: str | None = None
    last_ready_at: str | None = None
    vtty_path: str | None = None
    bridge: UARTBridge | None = None
    # attach probe 期間尚未公開給一般 session 操作的 bridge；RX quiet gate 仍須即時推送。
    attach_bridge: UARTBridge | None = dataclasses.field(default=None, repr=False)
    attached_real_path: str | None = None
    bridge_generation: int = 0
    # passthrough ATTACHED 狀態的最近 RX 時刻；僅記憶體使用，不對外公開或持久化。
    passthrough_quiet_since: float | None = None
    login_gen: int = 0
    _boot_quiet_lock: threading.RLock = dataclasses.field(
        default_factory=threading.RLock, repr=False
    )
    recovering: bool = False
    recovery_started_at: str | None = None
    pending_auto_login: bool = False
    interactive_session_id: str | None = None
    foreground_busy: bool = False
    _foreground_operation: _ForegroundOperation | None = dataclasses.field(default=None, repr=False)
    background_cmd_ids: list[str] = dataclasses.field(default_factory=list)
    active_capture: SessionCapture | None = None
    drain_stalled: bool = False
    drain_timeout_epoch: int | None = None
    _pending_drain: _DrainJob | None = dataclasses.field(default=None, repr=False)
    _release_persist_pending: bool = dataclasses.field(default=False, repr=False)
    retained_consoles: PreservedConsoles | None = None
    retained_human_owner: str | None = None
    retained_human_timeout_s: float | None = None
    fg_cmd_started_mono: float | None = None
    fg_cmd_expected_duration_s: float | None = None
    # Activity tracking (issue #34)
    last_state_change_at: str | None = None
    last_rx_at: str | None = None
    last_tx_at: str | None = None
    last_probe_at: str | None = None
    last_rx_mono: float = 0.0
    last_tx_mono: float = 0.0
    reprobe_attempts: int = 0
    next_reprobe_at: float | None = None
    reprobe_exhausted: bool = False
    # 帳密解析終態一次性示警去重（#140）：進入 CREDENTIALS_UNRESOLVED 已告警即設 True，
    # 抑制 recovery 迴圈逐輪重複告警；手動 attach/recover（重新介入）時重置為 False。
    credentials_warned: bool = False
    # last_error 的人類可讀補充（#150）：目前僅 TRANSPORT_STALL 會帶 host 層復原提示；
    # last_error property setter 於值變更時自動清空，不殘留 stale detail。
    last_error_detail: str | None = None
    # transport stall 一次性示警去重（#150，仿 credentials_warned 慣例）：
    # 已告警即設 True；_mark_session_rx（RX 恢復）時重臂為 False。
    transport_stall_warned: bool = False
    # device handoff（issue #54）
    released_by: str | None = None
    released_at: str | None = None
    released_reason: str | None = None
    # OD-63：template 已不存在的 RELEASED 紀錄所實體化的佔位（passthrough、不可下命令）；
    # device attach 時移除自身並交 template 偵測建立正式 session。
    released_placeholder: bool = False
    # 動態 profile 來源（#95）：unknown / pin / sticky / detected / fallback / yaml-target
    profile_source: str = "unknown"
    # U-Boot autoboot 保護（#130）：boot quiet window 截止時刻（monotonic；0.0=未啟用）。
    # 視窗內 gate 以下所有自動 system probe TX（#130 review 收斂後的完整清單，逐一在各自
    # 呼叫點呼叫 boot_quiet_active()）：
    #   - _handle_reboot_command 觸發的 _spawn_reboot_recovery（reboot recovery 迴圈）
    #   - _prepare_reprobe_locked / _reprobe_target_still_valid_locked（readiness reprobe）
    #   - _attach_by_id（attach probe，含 DUT reboot 期間裝置 re-enumerate 觸發的 re-attach）
    #   - _probe_existing_bridge（attach_session ATTACHED 分支與 recover_session ATTACHED
    #     分支共用的唯一 probe 入口）
    #   - _recover_after_failure 的 CTRL_C/CTRL_D 強制按鍵迴圈（agent 命令逾時觸發）
    #   - _self_test_impl 的 READY 分支 nonce probe
    # **絕不** gate human console bytes 與 interactive lease TX（與 #114「刻意進
    # bootloader」相容：human/lease 送鍵永遠放行）。
    # #130 Finding 4 已由 #139 收斂：本欄位仍**不**改動 session.state，但 agent 顯式
    # 命令（command.submit／file.push／file.pull）在「quiet armed 且 session 尚未重新
    # 確認 READY」的過渡態被雙層 gate（submit-time：service._resolve_session_id；
    # execute-time：execute_command／file_push／file_pull）以可重試的 AUTOBOOT_QUIET
    # 即時拒絕、零 UART 副作用；任一 READY 轉移點落定時同步 clear_boot_quiet()，
    # 故 session 回 READY 後 agent 顯式命令永遠放行。
    boot_quiet_until: float = 0.0
    # banner 偵測用 rolling RX tail（跨 chunk 邊界）；純內部狀態，不出 to_public_dict。
    boot_banner_tail: str = ""
    # READY 再確認 pending（#162）：arm quiet 即置 True，僅各 READY 確認點
    # （attach probe／reboot recovery／self-test nonce 的 confirm_ready()）清除——
    # quiet 過期或 RX prompt 解除都「不」清。transient，不進 _save_state。
    # 根因：prpl/OpenWrt askconsole 停在「Please press Enter to activate this
    # console」，既不匹配 login_regex 也不匹配 prompt_regex → profile 未設
    # boot_done_regex 時 quiet 只能靠 180s 過期清空；期間 session 名義 READY 且
    # 無人重新確認，第一個 agent 命令的 "\n" 觸發 askconsole 啟用、命令文字被
    # askfirst 吞掉、stdout 吃到啟用 banner。
    # agent 顯式命令 gate 改判 agent_gate_active()（quiet or pending），解除的
    # 唯一路徑＝READY 經 nonce probe 再確認；確認 probe 的 "\n" 順帶消耗 banner。
    ready_reconfirm_pending: bool = False
    # pending 的有界化欄位（#162 回歸修）：pending 若無 deadline／無 attempt 上限／
    # 無失敗終態，`AUTOBOOT_QUIET`（可重試）會變成「永遠不可能成功的可重試錯誤」——
    # 只要確認 probe 的任一守衛長期不成立（reprobe_exhausted latch／RX 洪水／human
    # 持續活躍／banner 週期性重臂），pending 就永久 True，把 agent driver 的重試迴圈
    # 拖進無界迴圈。deadline 於「首次」置位時設定，**重臂不延長**（否則週期性 banner
    # 可把期限推到無限）。逾時或逾次由 expire_ready_reconfirm() 落 ready_reconfirm_failed
    # 終態（不可重試的 READY_UNCONFIRMED）。三者皆 transient，不進 _save_state。
    ready_reconfirm_deadline: float = 0.0
    ready_reconfirm_attempts: int = 0
    ready_reconfirm_failed: bool = False
    # MCU 燒錄狀態（issue #55）：僅 runtime transient，不寫 _save_state / to_public_dict
    flash_prev_state: str | None = None
    # recovery lease stash（Phase B issue #44）
    _stashed_human_lease: InteractiveLease | None = dataclasses.field(default=None, repr=False)
    _state: str = dataclasses.field(default="DETACHED", init=False, repr=False)
    _last_error: str | None = dataclasses.field(default=None, init=False, repr=False)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        # 值變更時自動清 detail（#150）：detail 永遠只描述當前 last_error，
        # 呼叫端如需附 detail，須在設定 last_error **之後**再設 last_error_detail。
        prev = getattr(self, "_last_error", None)
        if prev != value:
            self.last_error_detail = None
        self._last_error = value

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        prev = getattr(self, "_state", None)
        if prev is not None and prev != value:
            self.last_state_change_at = now_iso()
            if value == "ATTACHED" and self.profile.platform == "passthrough":
                self.passthrough_quiet_since = time.monotonic()
            elif value != "ATTACHED":
                self.passthrough_quiet_since = None
        elif (
            value == "ATTACHED"
            and self.profile.platform == "passthrough"
            and self.passthrough_quiet_since is None
        ):
            self.passthrough_quiet_since = time.monotonic()
        self._state = value

    def compute_idle_ms(self) -> int | None:
        last = max(self.last_rx_mono, self.last_tx_mono)
        if last == 0.0:
            return None
        return int((time.monotonic() - last) * 1000)

    def classify_activity(self) -> str:
        if self._state not in ("READY", "ATTACHED"):
            return "offline"
        idle = self.compute_idle_ms()
        if idle is None:
            return "newly-attached"
        if idle < 5_000:
            return "active"
        if idle < 60_000:
            return "idle-healthy"
        return "quiet-suspicious"

    def reset_reprobe_progress(self) -> None:
        self.reprobe_attempts = 0
        self.next_reprobe_at = None
        self.reprobe_exhausted = False

    def boot_quiet_active(self, now: float | None = None) -> bool:
        """boot quiet window 是否進行中（#130）。"""
        with self._boot_quiet_lock:
            if self.boot_quiet_until <= 0.0:
                return False
            return (time.monotonic() if now is None else now) < self.boot_quiet_until

    def arm_boot_quiet(self, now: float | None = None) -> None:
        """啟用／延長 boot quiet window，並清空 rolling tail（#130）。

        清 tail 是必要的：reboot 命令送出當下，tail 可能還殘留送出前的 shell
        prompt，若不清掉，下一個 RX chunk 的解除檢查會被舊 prompt 誤觸發、
        quiet window 立即失效。
        """
        with self._boot_quiet_lock:
            target = self.bridge or self.attach_bridge
            invalidate_deferred = getattr(target, "invalidate_deferred", None)
            if callable(invalidate_deferred):
                invalidate_deferred("boot_quiet")
            base = time.monotonic() if now is None else now
            self.boot_quiet_until = base + BOOT_QUIET_WINDOW_S
            if not self.profile.console_unlock_env:
                self.login_gen += 1
            self.boot_banner_tail = ""
            # #162：一旦疑似重開機，agent gate 需等 READY 經 nonce probe 再確認才解除。
            # **重臂不延長 deadline**：banner／倒數行會在整段開機過程反覆抵達（每次都
            # 呼叫本函式），若每次都把期限往後推，pending 的有界性即失效——週期性出現
            # 字面 "U-Boot" 的 RX（cat /proc/cmdline、dmesg…）就能讓 gate 永久 armed。
            # 期限只在「pending 由 False 翻 True」的那一刻起算。
            if not self.ready_reconfirm_pending:
                self.ready_reconfirm_pending = True
                self.ready_reconfirm_deadline = base + READY_RECONFIRM_MAX_S
                self.ready_reconfirm_attempts = 0
                # 新一輪疑似重開機＝新的確認週期，清掉上一輪的終態旗標。
                self.ready_reconfirm_failed = False
            self._push_probe_gate()
            self._push_tx_gate()

    def clear_boot_quiet(self) -> None:
        """解除 boot quiet window 並清空 rolling tail（#130）。

        僅解除 TX 靜默維度；**不**清 ``ready_reconfirm_pending``（#162）——
        agent gate 的解除一律走 ``confirm_ready()``。
        """
        with self._boot_quiet_lock:
            self.boot_quiet_until = 0.0
            self.boot_banner_tail = ""
            self._push_probe_gate()
            self._push_tx_gate()

    def confirm_ready(self) -> None:
        """READY 確認落定（#162）：清 quiet 與 agent gate。

        由各 READY 確認點呼叫（attach probe／reboot recovery／self-test nonce
        成功）；passthrough 另以核准的 RX 靜默時間窗作為 READY 判定，不送確認 probe。
        **不**包含 reboot 命令 2s 內 prompt 回顯（見 ``_handle_reboot_command``）：
        prpl/OpenWrt 的 ``reboot`` 是非同步的，shell 會立刻重印 prompt，該回顯不是 READY 實證。

        清 pending 的同時清掉全部有界化欄位，維持「成功是唯一正常出口」語意。
        """
        with self._boot_quiet_lock:
            self.clear_boot_quiet()
            self.ready_reconfirm_pending = False
            self.ready_reconfirm_deadline = 0.0
            self.ready_reconfirm_attempts = 0
            self.ready_reconfirm_failed = False
            self._push_tx_gate()

    def reset_passthrough_boot_gate(self) -> None:
        """passthrough 重掛時清除前一 profile 留下的 boot quiet／READY gate。"""
        with self._boot_quiet_lock:
            self.boot_quiet_until = 0.0
            self.boot_banner_tail = ""
            self.ready_reconfirm_pending = False
            self.ready_reconfirm_deadline = 0.0
            self.ready_reconfirm_attempts = 0
            self.ready_reconfirm_failed = False
            self._push_probe_gate()
            self._push_tx_gate()

    def expire_ready_reconfirm(self, now: float | None = None, *, force: bool = False) -> bool:
        """pending 逾時／逾次 → 落 ``ready_reconfirm_failed`` 終態（#162 有界化）。

        這是 ``ready_reconfirm_pending`` 的**第二個、也是最後一個** clearer
        （另一個是成功路徑的 ``confirm_ready()``）。回傳本次是否真的翻轉，供呼叫端
        決定是否要記事件／落 last_error。

        ``force=True``：已有確定證據（如 RX tail 命中 bootloader prompt）證明自動
        確認不可能成功時，跳過時間／次數判定直接落終態。
        """
        with self._boot_quiet_lock:
            if not self.ready_reconfirm_pending:
                return False
            base = time.monotonic() if now is None else now
            if not force:
                deadline_hit = self.ready_reconfirm_deadline > 0.0 and base >= self.ready_reconfirm_deadline
                attempts_hit = self.ready_reconfirm_attempts >= READY_RECONFIRM_MAX_ATTEMPTS
                if not deadline_hit and not attempts_hit:
                    return False
            self.ready_reconfirm_pending = False
            self.ready_reconfirm_failed = True
            self._push_tx_gate()
            return True

    def _push_probe_gate(self, bridge: UARTBridge | None = None) -> None:
        with self._boot_quiet_lock:
            target = bridge or self.bridge or self.attach_bridge
            setter = getattr(target, "set_probe_gate", None) if target is not None else None
            if callable(setter):
                setter(
                    quiet_until=(
                        self.boot_quiet_until
                        if not self.profile.console_unlock_env
                        else 0.0
                    ),
                    login_gen_floor=self.login_gen,
                    self_test_quiet_until=self.boot_quiet_until,
                )

    def _push_tx_gate(self, bridge: UARTBridge | None = None) -> None:
        with self._boot_quiet_lock:
            target = bridge or self.bridge or self.attach_bridge
            setter = getattr(target, "set_tx_gate", None) if target is not None else None
            if callable(setter):
                setter(
                    generation=self.bridge_generation,
                    quiet=self.agent_gate_active(),
                    lease_owner=(
                        f"interactive:{self.interactive_session_id}"
                        if self.interactive_session_id is not None
                        else None
                    ),
                )

    def ready_reconfirm_remaining_s(self, now: float | None = None) -> float | None:
        """pending 距 deadline 的剩餘秒數；非 pending／無 deadline 時 None。"""
        if not self.ready_reconfirm_pending or self.ready_reconfirm_deadline <= 0.0:
            return None
        base = time.monotonic() if now is None else now
        return round(max(self.ready_reconfirm_deadline - base, 0.0), 1)

    def agent_gate_active(self, now: float | None = None) -> bool:
        """agent 顯式命令 gate 的統一判定（#162）。

        quiet 進行中 **或** READY 再確認 pending 皆須 gate——quiet 過期只代表
        「180s 沒再看到 banner」，不代表 session 已重新確認可下命令。

        判定前先跑 ``expire_ready_reconfirm()``：pending 逾時／逾次即翻終態，之後
        本函式對它回 False，由呼叫端改回不可重試的 ``READY_UNCONFIRMED``。
        """
        if self.profile.platform == "passthrough":
            return False
        self.expire_ready_reconfirm(now)
        return self.boot_quiet_active(now) or self.ready_reconfirm_pending

    def to_public_dict(self) -> dict[str, Any]:
        console_count = 0
        console_endpoint = None
        if self.bridge is not None:
            console_count = len(self.bridge.list_consoles())
            console_endpoint = self.bridge.console_endpoint()
        elif self.retained_consoles is not None:
            console_count = len(self.retained_consoles.clients)
        # RX 速率指標（#153）：getattr 防禦——既有多個測試 FakeBridge 無 rx_stats，
        # 回傳非 dict（如 MagicMock）也一律視為不支援 → 欄位為 None。
        rx_stats: dict[str, Any] | None = None
        rx_stats_fn = getattr(self.bridge, "rx_stats", None) if self.bridge is not None else None
        if callable(rx_stats_fn):
            try:
                candidate = rx_stats_fn()
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                rx_stats = candidate
        now_mono = time.monotonic()
        vtty_path = self.vtty_path
        if vtty_path is None and self.retained_consoles is not None:
            vtty_path = self.retained_consoles.primary_vtty()
        outstanding = len(self.background_cmd_ids) + (1 if self.foreground_busy else 0)
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "profile": self.profile.profile_name,
            "com": self.profile.com,
            "alias": self.profile.alias,
            "act_no": self.profile.act_no,
            "device_by_id": self.profile.device_by_id,
            "platform": self.profile.platform,
            "profile_source": self.profile_source,
            "command_capable": self.profile.command_capable,
            "state": self.state,
            "last_error": self.last_error,
            # last_error 的人類可讀補充（#150）：TRANSPORT_STALL 時帶 host 層復原提示。
            "last_error_detail": self.last_error_detail,
            "detached_at": self.detached_at,
            "last_ready_at": self.last_ready_at,
            "vtty": vtty_path,
            "attached_real_path": self.attached_real_path,
            "bridge_generation": self.bridge_generation,
            "recovering": self.recovering,
            "interactive_session_id": self.interactive_session_id,
            "foreground_busy": self.foreground_busy,
            "fg_cmd_expected_duration_s": self.fg_cmd_expected_duration_s,
            "console_count": console_count,
            "last_state_change_at": self.last_state_change_at,
            "last_rx_at": self.last_rx_at,
            "last_tx_at": self.last_tx_at,
            "last_probe_at": self.last_probe_at,
            "reprobe_attempts": self.reprobe_attempts,
            "next_reprobe_at": self.next_reprobe_at,
            "reprobe_exhausted": self.reprobe_exhausted,
            # #130：boot quiet window 剩餘秒數（None=未啟用/已解除；monotonic 差值，
            # 跨程序可讀）。供 doctor / session list 觀測 autoboot 保護狀態。
            "boot_quiet_remaining_s": (
                round(self.boot_quiet_until - time.monotonic(), 1)
                if self.boot_quiet_until > 0.0 and self.boot_quiet_until > time.monotonic()
                else None
            ),
            # #162：READY 再確認 pending（quiet 過期不清，READY 確認點才清）。
            # True 期間 agent 顯式命令被 AUTOBOOT_QUIET gate。
            "ready_reconfirm_pending": self.ready_reconfirm_pending,
            # #162 有界化：pending 逾時／逾次落定的終態旗標；True 時 agent 顯式命令
            # 改回**不可重試**的 READY_UNCONFIRMED（帶 recommended_action，不帶
            # retry_after_s）。remaining 供 operator 判讀還有多久落終態。
            "ready_reconfirm_failed": self.ready_reconfirm_failed,
            "ready_reconfirm_remaining_s": self.ready_reconfirm_remaining_s(),
            "idle_for_ms": self.compute_idle_ms(),
            # RX/TX 單邊年齡（#150）：idle_for_ms 取 max(rx,tx) 會被 probe/human TX 拉小，
            # stall（RX 單邊凍結）在 session list 看不出來；此兩欄讓 operator 一眼判讀。
            "last_rx_age_s": (
                round(now_mono - self.last_rx_mono, 1) if self.last_rx_mono > 0.0 else None
            ),
            "last_tx_age_s": (
                round(now_mono - self.last_tx_mono, 1) if self.last_tx_mono > 0.0 else None
            ),
            # RX 速率指標（#153）：bridge 無/不支援 rx_stats 時為 None。
            "rx_bytes_last_10s": rx_stats.get("rx_bytes_last_10s") if rx_stats else None,
            "rx_rate_bps": rx_stats.get("rx_rate_bps") if rx_stats else None,
            "outstanding_commands": outstanding,
            "activity_classification": self.classify_activity(),
            "released_by": self.released_by,
            "released_at": self.released_at,
            "released_reason": self.released_reason,
            "capture": {
                "capture_id": self.active_capture.capture_id,
                "log_path": self.active_capture.log_path,
                "status": self.active_capture.status,
                "line_count": self.active_capture.line_count,
                "byte_count": self.active_capture.byte_count,
            } if self.active_capture else None,
            "drain_stalled": self.drain_stalled,
            "drain_timeout_epoch": self.drain_timeout_epoch,
        }
        # Windows TCP console 連線端點（#131 點 3）：無 PTY 平台恆輸出（bridge 未起時
        # 為 null，schema 跨 session 狀態穩定，consumer 不需處理 key 忽有忽無）；
        # POSIX（有 PTY）不加 key → session 輸出逐位元組不變。
        if console_endpoint is not None or not _pty_available():
            payload["console_endpoint"] = console_endpoint
        return payload


class SessionManager:
    def __init__(
        self,
        profiles: list[SessionProfile],
        wal: WalWriter,
        *,
        templates: list[ProfileTemplate] | None = None,
        max_sessions: int = 16,
        on_ready: Callable[[str], None],
        on_detached: Callable[[str], None],
        on_console_line: Callable[[str, str, str], None] | None = None,
        on_command_flush: Callable[[str, str], None] | None = None,
        state_path: str | None = None,
        admission: Any | None = None,
    ) -> None:
        # state.json 路徑注入（#120）：daemon 走 default（模組層 STATE_PATH），測試注入 tmp。
        # fallback 於「建構時」讀模組層全域（非 def-time default），使既有
        # setattr(session_manager, "STATE_PATH", ...) 的測試隔離手法持續有效。
        self._state_path = STATE_PATH if state_path is None else state_path
        self._wal = wal
        self._on_ready = on_ready
        self._on_detached = on_detached
        self._on_console_line = on_console_line
        self._on_command_flush = on_command_flush
        self._admission = admission
        self._lock = threading.RLock()
        # state.json 寫入序列化（#133）：多 attach 執行緒並發 os.replace 同一目的檔在
        # Windows 會 WinError 5；獨立於 self._lock（snapshot 用），I/O 段專用、不巢狀他鎖。
        self._state_io_lock = threading.Lock()
        self._rx_observers: list[Callable[[str, bytes, int], None]] = []
        self._sessions: dict[str, SessionRuntime] = {}
        self._aliases = AliasRegistry()
        self._devices: dict[str, DeviceInfo] = {}
        self._binding_overrides: dict[str, str] = {}
        # startup 確定性 COM rank（#100）：by_id → 預配 COM 號。
        # 由 prepare_dynamic_rank 在 lock 內、spawn attach 前一次配好；
        # _session_from_template 建立 dynamic session 時優先消費（pop）這裡的預配號。
        self._pending_com: dict[str, str] = {}
        self._attach_inflight: set[str] = set()
        self._attach_threads: dict[str, threading.Thread] = {}
        self._attach_ops: dict[str, _AttachOp] = {}
        self._preopened_ports: dict[str, _AttachOp] = {}
        self._active_attach_ops: dict[str, _AttachOp] = {}
        self._attach_bridges: dict[str, UARTBridge] = {}
        self._attach_start_events: dict[str, threading.Event] = {}
        # readiness 自動重探（#69）：_reprobe_inflight 防止同一 session 重複 spawn auto worker；
        # _reprobe_probe_locks 為 per-session probe 互斥（auto 用 non-blocking、manual attach/recover
        # 用 blocking），避免 auto 與 manual 在同一 bridge 上並發 probe（Finding 2）。
        self._reprobe_inflight: set[str] = set()
        self._reprobe_probe_locks: dict[str, threading.Lock] = {}
        self._reprobe_workers: list[threading.Thread] = []
        self._released_by_ids: set[str] = set()
        self._draining_by_ids: dict[str, _DrainJob] = {}
        self._aborted_cmd_ids: set[str] = set()
        self._shutting_down = False
        # 交接停止（SC-11 `daemon.handoff_stop`）freeze 成立後為 True：DeviceWatcher tick 不再排工、
        # 在途 readiness probe 的結果不再改寫 session（見 freeze_for_handoff）。
        self._handoff_frozen = False
        self._shutdown_deadline: float | None = None
        self._shutdown_workers: set[threading.Thread] = set()
        # flash 偵測 probe 進行中的 COM（#83 RACE-2 final）：probe 已開 bridge flash_mode gate 擋 console，
        # 但 enter_flashing 之前 session.state 仍是 READY/ATTACHED，destructive op（clear/release/bind）只
        # 守 state==FLASHING → 探測窗口內仍可 detach/rebind bridge 中斷 probe。此 set 在 probe 期間封鎖。
        self._flash_critical_coms: set[str] = set()
        self._loaded_released: dict[str, dict[str, Any]] = {}
        self._release_persist_events: dict[str, threading.Event] = {}
        self._background: dict[str, BackgroundCapture] = {}
        self._interactive: dict[str, InteractiveLease] = {}
        self._tombstones: dict[str, deque[str]] = {}
        self._interactive_post_close: dict[str, object] = {}
        self._cancelled_tx_cmd_ids: OrderedDict[str, None] = OrderedDict()
        self._capture_fps: dict[str, Any] = {}  # capture_id → open file object
        self._templates: list[ProfileTemplate] = list(templates) if templates else []
        self._max_sessions = max_sessions
        # 動態 profile 持久化（#95）：device_key → profile_name
        self._profile_pins: dict[str, str] = {}
        self._profile_detected: dict[str, str] = {}
        # 孤兒 console 回收節流（#76）：記錄上次共享 /proc 掃描的時間，避免每 tick 都掃一次 procfs。
        self._last_console_reap_at: float = 0.0

        self._load_state()
        # #186：一個 explicit YAML target 自己宣告的 device_by_id 對其他 target 而言必須排他。
        # 若某 session 的持久化 binding override 剛好等於「另一個 target 自己在 YAML 宣告」的
        # device_by_id（多半是舊版 _attach_by_id DETACHED-rebind fallback 在該 target 尚未配置
        # 前，把這顆當時無人認領的裝置誤借給別的 DETACHED target、又持久化下來的殘留紀錄——見
        # issue #186），這筆 override 已不具權威性：這顆裝置現在有明確主人，不能再讓別的 target
        # 頂著這筆 override 繼續佔用它。改用該 target 自己的 YAML 宣告值，並清掉這筆過期 override，
        # 避免下次載入再誤用；同時讓「哪個 target 真正拿到這顆裝置」不再取決於 profile 檔案的
        # 載入順序（self._sessions insertion order 只在後面 exact-match 時當 tiebreak）。
        declared_by_ids = {p.device_by_id for p in profiles if p.device_by_id}
        for p in profiles:
            sid = f"{p.profile_name}:{p.com}"
            override = self._binding_overrides.get(sid)
            if override and override != p.device_by_id and override in declared_by_ids:
                del self._binding_overrides[sid]
                override = None
            device_by_id = override if override else p.device_by_id
            if not device_by_id:
                continue
            profile = dataclasses.replace(p, device_by_id=device_by_id)
            if sid not in self._sessions:
                self._sessions[sid] = SessionRuntime(session_id=sid, profile=profile)
                self._sessions[sid].profile_source = "yaml-target"
            self._aliases.set_for_session(sid, profile.alias)
        for sid, meta in list(self._loaded_released.items()):
            s = self._sessions.get(sid)
            if s is None:
                by_id = meta.get("by_id")
                if not isinstance(by_id, str) or not by_id:
                    import logging

                    logging.getLogger("serialwrap").warning(
                        "released session 無法還原（session=%s、缺少 by_id），保留持久化阻擋",
                        sid,
                    )
                    continue
                sid_profile, _, sid_com = sid.rpartition(":")
                pinned_name = self._profile_pins.get(by_id)
                detected_name = self._profile_detected.get(by_id)
                template_name = pinned_name or detected_name or sid_profile
                tpl = self._template_by_name(template_name) or self._template_by_name(sid_profile)
                placeholder = False
                if tpl is None:
                    # 舊 state.json 不保存 template 內容。通用 passthrough 名稱直接以最小
                    # profile 還原；其餘 template 已不存在者（OD-63）實體化為 RELEASED 佔位
                    # （passthrough、不可下命令），只供 device attach 解除阻擋：解除時佔位
                    # 移除自身並交 template 偵測建立正式 session，不得降級成另一種 profile。
                    placeholder = sid_profile not in {"passthrough", "others-template"}
                    if placeholder:
                        import logging

                        logging.getLogger("serialwrap").warning(
                            "released session 的 template 不存在（session=%s、profile=%s），保留持久化阻擋",
                            sid,
                            template_name,
                        )
                    tpl = ProfileTemplate(
                        profile_name=sid_profile,
                        platform="passthrough",
                        ready_probe="",
                    )
                com = sid_com
                if not isinstance(com, str) or not com:
                    import logging

                    logging.getLogger("serialwrap").warning(
                        "released session 無法還原（session=%s、缺少 COM），保留持久化阻擋",
                        sid,
                    )
                    continue
                aliases = self._aliases.dump()
                alias = next(
                    (name for name, row in aliases.items() if row.get("session_id") == sid),
                    None,
                )
                act_no: int | None = None
                if isinstance(alias, str) and alias.startswith(f"{tpl.profile_name}+"):
                    suffix = alias.rsplit("+", 1)[-1]
                    if suffix.isdigit():
                        act_no = int(suffix)
                if act_no is None:
                    match = re.fullmatch(r"COM(\d+)", com, flags=re.IGNORECASE)
                    act_no = int(match.group(1)) + 1 if match is not None else len(self._sessions) + 1
                if not isinstance(alias, str) or not alias:
                    alias = f"{tpl.profile_name}+{act_no}"
                s = self._session_from_template(
                    tpl,
                    by_id,
                    com_override=com,
                    act_no_override=act_no,
                    alias_override=alias,
                    session_id_override=sid,
                )
                source = "pin" if pinned_name else "detected" if detected_name else "fallback"
                s.profile_source = source
                s.released_placeholder = placeholder
            if s is not None:
                s.state = "RELEASED"
                s.released_by = meta.get("released_by")
                s.released_at = meta.get("released_at")
                s.released_reason = meta.get("reason")
                self._loaded_released.pop(sid, None)
        self._save_state()

    def begin_shutdown(self) -> None:
        """凍結 attach、hotplug 與後置重開入口。"""
        with self._lock:
            self._shutting_down = True

    def freeze_for_handoff(self) -> list[str]:
        """交接停止成功後凍結背景引擎與既有 bridge 的 TX（SC-11 `daemon.handoff_stop`，R1-F1）。

        RPC admission 只擋新進的 RPC；DeviceWatcher 重探 tick、重新 attach、login 與人工 console 行
        等背景來源原本不經 admission，排空窗口內仍會 TX 或改寫 session。本方法於 freeze 成立時：

        1. 進入 stopping gate（`begin_shutdown`）：attach、熱插不再開 UART、不建 worker；
        2. 設 `_handoff_frozen`：`reconcile_readiness` 不再排重探／重 attach 與 lease tick，
           在途 readiness probe 的結果不再改寫 session；
        3. 對所有已掛載 bridge（session、attach、probe）呼叫 `freeze_tx()`：此後任何來源的 TX
           都由 bridge 拒絕（零 TX）。

        回傳 TX fence 未在期限內收斂的 COM 清單（空清單＝凍結成立，回覆前已授權的寫入段皆已結束）。
        收斂失敗且本次為新凍結時，撤銷上述三項（flag 還原、bridge 凍結撤銷），daemon 回到可正常運作的狀態，
        由 `handoff_stop` 回 SESSION_BUSY（SC-11：忙碌不凍結、不停止）。已凍結者保持凍結（冪等）。

        reader 與埠保持開啟；實際排空仍由 stop 的 `shutdown_drain()` 於窗口結束後完成。
        """
        with self._lock:
            was_frozen = self._handoff_frozen
            was_shutting_down = self._shutting_down
            self._handoff_frozen = True
            self._shutting_down = True
            bridges: dict[int, UARTBridge] = {}
            for session in self._sessions.values():
                if session.bridge is not None:
                    bridges[id(session.bridge)] = session.bridge
            for bridge in self._attach_bridges.values():
                bridges[id(bridge)] = bridge
            for table in (self._attach_ops, self._preopened_ports, self._active_attach_ops):
                for op in table.values():
                    if op.probe_bridge is not None:
                        bridges[id(op.probe_bridge)] = op.probe_bridge
            unconverged: list[str] = []
            for bridge in bridges.values():
                freeze_tx = getattr(bridge, "freeze_tx", None)
                if callable(freeze_tx) and freeze_tx() is False:
                    unconverged.append(str(getattr(bridge, "com", "") or ""))
            if unconverged and not was_frozen:
                # 交接未成立：撤銷本次新建立的凍結。drain 已開始的 bridge 由 thaw_tx 自行保留凍結。
                self._handoff_frozen = False
                self._shutting_down = was_shutting_down
                for bridge in bridges.values():
                    thaw_tx = getattr(bridge, "thaw_tx", None)
                    if callable(thaw_tx):
                        thaw_tx()
            return sorted(set(unconverged))

    def shutdown_drain(self, *, total_timeout_s: float = 5.0) -> dict[str, Any]:
        """以一個共用期限等待 attach 收尾並取得所有 bridge 關閉回條。"""
        self.begin_shutdown()
        deadline = time.monotonic() + max(0.0, total_timeout_s)
        with self._lock:
            self._shutdown_deadline = deadline
            attach_ops = {
                id(op): op
                for table in (self._attach_ops, self._preopened_ports, self._active_attach_ops)
                for op in table.values()
            }
            for op in attach_ops.values():
                op.cancelled.set()
                if op.probe_bridge is not None:
                    op.probe_bridge.begin_drain(
                        op.bridge_generation,
                        preserve_consoles=False,
                        deadline=deadline,
                    )
            for by_id, bridge in list(self._attach_bridges.items()):
                if by_id in self._draining_by_ids:
                    continue
                session = next(
                    (row for row in self._sessions.values() if row.profile.device_by_id == by_id),
                    None,
                )
                if session is not None:
                    self._begin_attach_bridge_stop_locked(
                        session,
                        by_id,
                        bridge,
                        preserve_consoles=True,
                        started_at=time.monotonic(),
                    )
            attach_threads = sorted(self._attach_threads.items())
        stalled: list[str] = []
        for by_id, worker in attach_threads:
            if worker is threading.current_thread():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                stalled.append(f"attach:{by_id}")

        for op in attach_ops.values():
            with self._lock:
                unconsumed = (
                    self._preopened_ports.get(op.by_id) is op
                    and self._active_attach_ops.get(op.by_id) is not op
                    and self._attach_ops.get(op.by_id) is not op
                )
            if unconsumed:
                self._finish_attach_op(op)
                continue
            if not op.done.wait(max(0.0, deadline - time.monotonic())):
                stalled.append(f"attachop:{op.by_id}")

        with self._lock:
            unresolved_ops = {
                id(op): op
                for table in (self._attach_ops, self._preopened_ports, self._active_attach_ops)
                for op in table.values()
            }
        for op in unresolved_ops.values():
            if not op.done.is_set():
                stalled.append(f"attachop:{op.by_id}")

        jobs: list[_DrainJob] = []
        attach_cleanup_jobs: list[_DrainJob] = []
        with self._lock:
            for session in self._sessions.values():
                if session._pending_drain is not None:
                    job = session._pending_drain
                elif session.bridge is not None:
                    job = self._begin_drain_locked(
                        session,
                        kind="shutdown",
                        reason="",
                        drop_consoles=False,
                        started_at=deadline,
                        deadline=deadline,
                    )
                else:
                    job = None
                if job is not None and not any(existing is job for existing in jobs):
                    jobs.append(job)
            attach_cleanup_jobs = [
                job for job in self._draining_by_ids.values()
                if job.kind == "attach_cleanup"
            ]
        for job in jobs:
            receipt = self._await_drain(job)
            if receipt is None:
                job.done.wait(max(0.0, deadline - time.monotonic()))
                if not job.done.is_set() or not self._receipt_is_safe(job.receipt):
                    stalled.append(job.session_id)
        for job in attach_cleanup_jobs:
            self._finish_attach_bridge_stop(job)
            if not job.done.is_set():
                job.done.wait(max(0.0, deadline - time.monotonic()))
            if not job.done.is_set() or not self._receipt_is_safe(job.receipt):
                stalled.append(f"attach:{job.session_id}")
        return {"graceful": not stalled, "stalled": sorted(set(stalled))}

    def _track_attach_bridge_stop(
        self,
        session: SessionRuntime,
        by_id: str,
        bridge: UARTBridge,
        *,
        preserve_consoles: bool = True,
    ) -> Any | None:
        """將 attach 失敗清理納入 by-id 屏障，避免回條前重新開啟裝置。"""
        if not callable(getattr(bridge, "begin_drain", None)) or not callable(
            getattr(bridge, "finish_drain", None)
        ):
            return bridge.stop(preserve_consoles=preserve_consoles)
        started_at = time.monotonic()
        with self._lock:
            job = self._begin_attach_bridge_stop_locked(
                session,
                by_id,
                bridge,
                preserve_consoles=preserve_consoles,
                started_at=started_at,
            )
        return self._finish_attach_bridge_stop(job)

    def _begin_attach_bridge_stop_locked(
        self,
        session: SessionRuntime,
        by_id: str,
        bridge: UARTBridge,
        *,
        preserve_consoles: bool,
        started_at: float,
    ) -> _DrainJob:
        job = _DrainJob(
            session_id=session.session_id,
            bridge=bridge,
            epoch=session.bridge_generation,
            kind="attach_cleanup",
            reason="ATTACH_CLEANUP",
            drop_consoles=not preserve_consoles,
            aborted_cmd_id=None,
            done=threading.Event(),
            deadline=started_at + DRAIN_TIMEOUT_MS / 1000,
                attach_started=self._attach_start_events.get(by_id)
                if self._attach_bridges.get(by_id) is bridge else None,
        )
        self._draining_by_ids[by_id] = job
        bridge.begin_drain(
            job.epoch,
            preserve_consoles=preserve_consoles,
            deadline=job.deadline,
        )
        return job

    def _finish_attach_bridge_stop(self, job: _DrainJob) -> Any | None:
        if job.attach_started is not None and not job.attach_started.wait(
            max(0.0, job.deadline - time.monotonic())
        ):
            return None
        receipt = job.bridge.finish_drain(
            max(0.0, job.deadline - time.monotonic()),
            on_late_receipt=lambda late: self._complete_attach_bridge_stop(job, late),
        )
        if receipt is not None and self._receipt_is_safe(receipt):
            self._complete_attach_bridge_stop(job, receipt)
            return receipt
        return None

    def _complete_attach_bridge_stop(self, job: _DrainJob, receipt: Any) -> None:
        if not self._receipt_is_safe(receipt):
            return
        with self._lock:
            by_id = next((key for key, value in self._draining_by_ids.items() if value is job), None)
            if by_id is None or job.done.is_set():
                return
            session = self._sessions.get(job.session_id)
            if session is not None:
                if session.attach_bridge is job.bridge:
                    session.attach_bridge = None
                if job.drop_consoles:
                    session.retained_consoles = None
                    session.retained_human_owner = None
                    session.retained_human_timeout_s = None
                else:
                    self._store_retained_consoles_locked(
                        session,
                        getattr(receipt, "preserved", None),
                        human_owner=session.retained_human_owner,
                        human_timeout_s=session.retained_human_timeout_s,
                    )
            self._draining_by_ids.pop(by_id, None)
            if self._attach_bridges.get(by_id) is job.bridge:
                self._attach_bridges.pop(by_id, None)
                self._attach_start_events.pop(by_id, None)
            job.receipt = receipt
            job.done.set()

    def _load_state(self) -> None:
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "rb") as fp:
                raw = fp.read()
        except OSError as exc:
            # 讀取既有 state.json 失敗（PermissionError／暫時性 EIO／fd 耗盡等）——檔案內容多半仍完好，
            # 只是此刻讀不到。絕不可當成「損毀」而備份/清空：__init__ 尾段的 _save_state() 會以空的
            # 記憶體狀態覆寫磁碟檔，遺失 RELEASED（裝置交接）→ 重啟後 daemon 重新 attach 已交給
            # flasher／人類的 tty，正是 CLAUDE.md 一再防範的 two-reader（#82 / Codex 必修）。
            # 故 fail closed：拋 StateLoadError 由 daemon 啟動層拒絕啟動，由運維修復後再起，
            # 而非帶病啟動 clobber 交接狀態。原檔絕不動。
            raise StateLoadError(
                f"無法讀取既有 state.json（{self._state_path}）：{exc}；"
                "為避免以空狀態覆寫並遺失 RELEASED 交接（two-reader 風險），daemon 拒絕啟動。"
            ) from exc
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            # 僅「確認的 JSON 格式損毀」（截斷／非 UTF-8／非 JSON）才備份重建：內容已無從復原 RELEASED，
            # 備份保留證據並清出 active 路徑供下次 _save_state 重建；原子寫入（本 PR）已使此情形罕見。
            # 與上方「讀取失敗」嚴格區分——後者一律 fail closed，不得在此誤判為損毀（#82 / Codex 必修）。
            import logging
            backup = f"{self._state_path}.corrupt"
            try:
                os.replace(self._state_path, backup)
            except OSError:
                backup = None
            logging.getLogger("serialwrap").warning(
                "state.json 解析失敗（JSON 損毀），略過載入並備份至 %s：%s", backup, exc
            )
            return
        rows = obj.get("aliases") if isinstance(obj, dict) else None
        if isinstance(rows, dict):
            self._aliases.load(rows)
        bindings = obj.get("bindings") if isinstance(obj, dict) else None
        if isinstance(bindings, dict):
            normalized: dict[str, str] = {}
            for sid, by_id in bindings.items():
                if isinstance(sid, str) and isinstance(by_id, str) and sid.strip() and by_id.strip():
                    normalized[sid.strip()] = by_id.strip()
            self._binding_overrides = normalized
        released = obj.get("released") if isinstance(obj, dict) else None
        if isinstance(released, dict):
            loaded: dict[str, dict[str, Any]] = {}
            for sid, meta in released.items():
                if not isinstance(sid, str) or not isinstance(meta, dict):
                    continue
                by_id = meta.get("by_id")
                loaded[sid] = {
                    "by_id": by_id,
                    "released_by": meta.get("released_by"),
                    "released_at": meta.get("released_at"),
                    "reason": meta.get("reason"),
                }
                if isinstance(by_id, str) and by_id:
                    self._released_by_ids.add(by_id)
            self._loaded_released = loaded
        pins = obj.get("profile_pins") if isinstance(obj, dict) else None
        if isinstance(pins, dict):
            self._profile_pins = {str(k).strip(): str(v).strip()
                                  for k, v in pins.items()
                                  if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
        detected = obj.get("profile_detected") if isinstance(obj, dict) else None
        if isinstance(detected, dict):
            self._profile_detected = {str(k).strip(): str(v).strip()
                                      for k, v in detected.items()
                                      if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}

    def _save_state(self) -> None:
        state_dir = os.path.dirname(self._state_path) or "."
        os.makedirs(state_dir, exist_ok=True)
        # snapshot 一律在 lock 內建（迭代 _sessions、複製 _binding_overrides/_profile_pins/
        # _profile_detected），避免並發 mutation 觸發 RuntimeError: dictionary changed size
        # during iteration 或寫出欄位彼此不一致的 state.json（Copilot review）；磁碟 I/O 留在
        # lock 外。self._lock 為 RLock，caller 已持鎖時可重入。
        with self._lock:
            released: dict[str, dict[str, Any]] = {}
            for sid, s in self._sessions.items():
                if s.state == "RELEASED":
                    released[sid] = {
                        "by_id": s.profile.device_by_id,
                        "released_by": s.released_by,
                        "released_at": s.released_at,
                        "reason": s.released_reason,
                    }
            for sid, meta in self._loaded_released.items():
                released.setdefault(sid, dict(meta))
            payload = json.dumps(
                {"aliases": self._aliases.dump(), "bindings": dict(self._binding_overrides),
                 "released": released, "profile_pins": dict(self._profile_pins),
                 "profile_detected": dict(self._profile_detected)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        # 原子寫入：temp + fsync + os.replace + 目錄 fsync（比照 wal.py 既有慣例），避免崩潰/斷電/ENOSPC
        # 在「直接覆寫 state.json」中途失敗留下截斷檔，致 _load_state 解析失敗而靜默全棄（含 RELEASED
        # 交接狀態）→ 重啟後 daemon 重新 attach 已交給 flasher/人類的 tty 形成 two-reader（#82）。
        # 每次取唯一 temp 名（mkstemp）：_save_state 可能由多執行緒並發呼叫（device 自動綁定／attach），
        # 固定 temp 名會在並發 os.replace 時互踩（FileNotFoundError）。
        # I/O 段以 _state_io_lock 序列化（#133）：唯一 temp 名只保證 temp 不互踩，
        # 並發 os.replace 到同一目的檔在 Windows 仍會 WinError 5（MoveFileEx 對被
        # 開啟的目的檔 access denied）；序列化後仍為 last-writer-wins（snapshot 在
        # self._lock 內建立，後進鎖者持較新快照）。鎖序 self._lock → _state_io_lock
        # 單向，I/O 段內不取其他鎖，無倒置。
        with self._state_io_lock:
            fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix="state.json.tmp.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                    fp.flush()
                    os.fsync(fp.fileno())
                # Windows 對外部讀者的瞬時 handle 另以退避重試兜底（#133）。
                _replace_state_file(tmp_path, self._state_path)
                # 目錄 fsync：確保 state.json 的目錄 entry 持久化（POSIX 語意）。
                # Windows（nt）：os.O_RDONLY 開目錄會拋 Permission Denied；NTFS 自行管理一致性，跳過即可（#84 PORT-4）。
                if sys.platform != "win32":
                    dir_fd = os.open(state_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
            finally:
                # os.replace 成功後 tmp 已不存在；中途失敗時清掉半寫 temp，且絕不動到原 state.json。
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
        _test_kill_after_save_hook(False)
    def _next_dynamic_com(self) -> str:
        """分配下一個可用的 COM 編號（須在 self._lock 內呼叫）。

        runtime 單片熱插（hotplug）的 fallback 路徑——純掃描第一個未被佔用的空號。
        startup 多片並發 attach 改走 prepare_dynamic_rank 預配（消除並發 race），不經此函式。

        不在此處以 max_sessions 設上限（避免全滿時 fallback 回已用/已 pending 的號）；
        max_sessions 由 attach 路徑另行把關。
        """
        used = {s.profile.com for s in self._sessions.values()}
        used |= set(self._pending_com.values())
        i = 0
        while f"COM{i}" in used:
            i += 1
        return f"COM{i}"

    def _by_path_for(self, by_id: str) -> str | None:
        """取得 by_id 對應的 by-path（須在 self._lock 內呼叫）。

        目前 ``DeviceInfo`` 無 by-path 欄位，FTDI 情境一律回 None；保留此 helper
        以利未來 by-path fallback（同款晶片去衝突）接上資料來源。
        """
        dev = self._devices.get(by_id)
        return getattr(dev, "by_path", None) if dev else None

    def _is_explicit_or_bound(self, by_id: str) -> bool:
        """by_id 是否為 explicit YAML target 或 binding override 綁定（須在 self._lock 內呼叫）。

        這類裝置為權威來源，COM 不由 dynamic rank 決定，排除在 rank pool 外。
        """
        if by_id in self._binding_overrides.values():
            return True
        return any(
            s.profile.device_by_id == by_id and s.profile_source == "yaml-target"
            for s in self._sessions.values()
        )

    def prepare_dynamic_rank(self, by_ids: list[str]) -> None:
        """startup：對整批在線 dynamic by-id 依 device_sort_key 排序，預配 COM 號（#100）。

        在 self._lock 內、spawn attach threads **之前**一次配好號，消除「誰先搶到 lock
        誰拿 COM0」的並發 race（restart 後 COM↔板對調的根因）。

        rank 作用域：explicit YAML target / binding override / RELEASED 的 by-id 與已有
        session 的 by-id 都排除在 pool 外（不污染既有持久化狀態）。預配號避開既有 session
        已佔用的 COM。
        """
        with self._lock:
            used = {s.profile.com for s in self._sessions.values()}
            used |= set(self._pending_com.values())
            ordered = sorted(
                (
                    b
                    for b in by_ids
                    if b
                    and b not in self._released_by_ids
                    and not self._is_explicit_or_bound(b)
                    and not any(
                        s.profile.device_by_id == b for s in self._sessions.values()
                    )
                ),
                key=lambda b: device_sort_key(b, self._by_path_for(b)),
            )
            idx = 0
            for b in ordered:
                if b in self._pending_com:
                    continue
                while f"COM{idx}" in used:
                    idx += 1
                com = f"COM{idx}"
                self._pending_com[b] = com
                used.add(com)
                idx += 1

    def com_for_by_id(self, by_id: str) -> str | None:
        """查詢 by_id 的 COM：既有 session 優先，否則回 startup 預配（pending）號。"""
        with self._lock:
            for s in self._sessions.values():
                if s.profile.device_by_id == by_id:
                    return s.profile.com
            return self._pending_com.get(by_id)

    def _session_from_template(
        self,
        tpl: ProfileTemplate,
        device_by_id: str,
        *,
        com_override: str | None = None,
        act_no_override: int | None = None,
        alias_override: str | None = None,
        session_id_override: str | None = None,
    ) -> SessionRuntime:
        """從 template 建立新的動態 session（須在 self._lock 內呼叫）。
        注意：profile_source 由呼叫方（_attach_by_id_dynamic）在回傳後設定，此處預設值不具權威性。
        """
        # startup 確定性 rank（#100）：優先用 prepare_dynamic_rank 預配的 COM 號（與
        # attach 完成順序無關），無預配（runtime 熱插）才 fallback 最低空號。
        com = com_override or self._pending_com.pop(device_by_id, None) or self._next_dynamic_com()
        act_no = act_no_override if act_no_override is not None else len(self._sessions) + 1
        alias = alias_override or f"{tpl.profile_name}+{act_no}"
        profile = SessionProfile(
            profile_name=tpl.profile_name,
            com=com,
            act_no=act_no,
            alias=alias,
            device_by_id=device_by_id,
            platform=tpl.platform,
            prompt_regex=tpl.prompt_regex,
            login_regex=tpl.login_regex,
            password_regex=tpl.password_regex,
            post_login_cmd=tpl.post_login_cmd,
            ready_probe=tpl.ready_probe,
            username=tpl.username,
            user_env=tpl.user_env,
            pass_env=tpl.pass_env,
            env_file=tpl.env_file,
            console_unlock_env=tpl.console_unlock_env,
            console_unlock_idle_s=tpl.console_unlock_idle_s,
            timeout_s=tpl.timeout_s,
            quiet_window_s=tpl.quiet_window_s,
            hard_timeout_s=tpl.hard_timeout_s,
            log_dir=tpl.log_dir,
            max_console_line_chars=tpl.max_console_line_chars,
            bootloader_prompts=tpl.bootloader_prompts,
            boot_done_regex=tpl.boot_done_regex,
            uart=tpl.uart,
        )
        sid = session_id_override or f"{profile.profile_name}:{com}"
        session = SessionRuntime(session_id=sid, profile=profile)
        self._sessions[sid] = session
        self._aliases.set_for_session(sid, alias)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [v.to_public_dict() for _, v in sorted(self._sessions.items())]

    def get_session(self, selector: str) -> SessionRuntime | None:
        with self._lock:
            if selector in self._sessions:
                return self._sessions[selector]
            for session in self._sessions.values():
                if selector == session.profile.com or selector == session.profile.alias:
                    return session
            return None

    def list_aliases(self) -> list[dict[str, Any]]:
        return self._aliases.list_alias()

    def active_cmd_id_for(self, com: str) -> str | None:
        with self._lock:
            session = next(
                (s for s in self._sessions.values() if s.profile.com == com), None
            )
            if session is None:
                return None
            return "foreground" if session.foreground_busy else None

    def profile_for(self, com: str) -> str | None:
        with self._lock:
            session = next(
                (s for s in self._sessions.values() if s.profile.com == com), None
            )
            if session is None:
                return None
            return session.profile.profile_name

    def known_coms(self) -> list[str]:
        with self._lock:
            return sorted(s.profile.com for s in self._sessions.values())

    def add_rx_observer(self, observer: Callable[[str, bytes, int], None]) -> None:
        with self._lock:
            self._rx_observers.append(observer)

    def _reset_reprobe_progress_locked(self, session: SessionRuntime) -> None:
        session.reset_reprobe_progress()

    @staticmethod
    def _set_bridge_interactive_admission(
        bridge: UARTBridge, allowed: bool
    ) -> list[tuple[str, bytes]]:
        """切換 bridge raw ownership gate；舊 fake bridge 沒有 seam 時維持相容。"""
        setter = getattr(bridge, "_set_interactive_admission", None)
        if callable(setter):
            result = setter(allowed)
            if isinstance(result, list):
                return result
        return []

    def _begin_foreground_operation_locked(
        self,
        session: SessionRuntime,
        *,
        kind: str = "command",
        cmd_id: str | None = None,
        expected_duration_s: float | None = None,
    ) -> tuple[_ForegroundOperation | None, dict[str, Any] | None]:
        """以 session/bridge 身分原子取得前景操作 admission。

        呼叫者必須已確認 session 可執行且持有 ``self._lock``。正常 command
        queue 仍由 arbiter 逐一呼叫此處；只有同一 session 的 direct manager
        writer 會在既有操作期間得到 ``SESSION_BUSY``。
        """
        bridge = session.bridge
        if bridge is None or session.foreground_busy or session._foreground_operation is not None:
            return None, {
                "ok": False,
                "error_code": "SESSION_BUSY",
                "selector": session.profile.com,
            }
        operation = _ForegroundOperation(
            session_id=session.session_id,
            bridge=bridge,
            bridge_generation=session.bridge_generation,
            cmd_id=cmd_id,
            kind=kind,
        )
        # 先關 bridge raw grant，再發佈 manager operation token；如此 TCP accept
        # 或 POSIX console attach 不會在 admission→實際 suspend 的窗口取得 raw。
        self._set_bridge_interactive_admission(bridge, False)
        session._foreground_operation = operation
        session.foreground_busy = True
        session.fg_cmd_started_mono = time.monotonic()
        session.fg_cmd_expected_duration_s = expected_duration_s
        return operation, None

    def _finish_foreground_operation(
        self,
        session: SessionRuntime,
        operation: _ForegroundOperation | None,
    ) -> None:
        """只清理仍屬於本 session/operation token 的 busy 狀態。

        舊 epoch callback 在 bridge 重建後即使晚到，也不得清除新 operation 的
        admission；detach/re-register 會先把 token 置空，這裡因此是安全 no-op。
        """
        if operation is None:
            return
        flush_data: list[tuple[str, bytes]] = []
        try:
            with self._lock:
                current = self._sessions.get(operation.session_id)
                if (
                    current is not session
                    or session._foreground_operation is not operation
                    or session.bridge is not operation.bridge
                    or session.bridge_generation != operation.bridge_generation
                ):
                    return
                # gate/owner 旗標與 deferred payload 在 manager lock 內擷取；真正 I/O
                # 放在 lock 外，token 則留到 replay 完成後才清除，避免全域 manager lock
                # 被背壓中的 UART write 卡住。
                flush_data = self._set_bridge_interactive_admission(operation.bridge, True)
            for source, payload in flush_data:
                client_id = source.partition(":")[2]
                permit = TxPermit(
                    f"console:{client_id}",
                    operation.bridge_generation,
                    "human_raw",
                    owner_gen=getattr(
                        payload,
                        "owner_gen",
                        getattr(operation.bridge, "owner_gen", 0),
                    ),
                )
                self._send_bytes_with_permit(
                    operation.bridge,
                    payload,
                    source=source,
                    cmd_id=None,
                    permit=permit,
                )
        finally:
            with self._lock:
                current = self._sessions.get(operation.session_id)
                if (
                    current is session
                    and session._foreground_operation is operation
                    and session.bridge is operation.bridge
                    and session.bridge_generation == operation.bridge_generation
                ):
                    session._foreground_operation = None
                    session.foreground_busy = False
                    session.fg_cmd_started_mono = None
                    session.fg_cmd_expected_duration_s = None

    @staticmethod
    def _foreground_operation_current_locked(
        session: SessionRuntime,
        bridge: UARTBridge,
        operation: _ForegroundOperation | None,
    ) -> bool:
        if session.bridge is not bridge or session._pending_drain is not None:
            return False
        if operation is None:
            return True
        return bool(
            session._foreground_operation is operation
            and operation.bridge is bridge
            and operation.bridge_generation == session.bridge_generation
        )

    # --- agent 顯式命令 gate（#139/#162）----------------------------------------
    def _agent_gate_reject_locked(self, session: SessionRuntime) -> dict[str, Any] | None:
        """execute_command／file_push／file_pull 共用的 gate 判定；須在 ``self._lock`` 內。

        回傳拒絕回應（dict）或 ``None``（放行）。兩種拒絕語意刻意分離：

        - ``AUTOBOOT_QUIET``（**可重試**，帶 ``retry_after_s``）：quiet window 進行中
          或 READY 再確認 pending 但仍在有界期限內——daemon 有自動路徑會解（RX 見
          prompt／確認 probe 成功），呼叫端等一下重送即可。
        - ``READY_UNCONFIRMED``（**不可重試**，帶 ``recommended_action``）：pending 已
          逾 ``READY_RECONFIRM_MAX_S`` 或逾 ``READY_RECONFIRM_MAX_ATTEMPTS``，或已由
          bootloader 證據直接落終態——daemon 已無自動路徑，繼續重試只會無界迴圈。
        """
        if session.profile.platform == "passthrough":
            return None
        # 先評估有界化終態：agent_gate_active() 內部亦會呼叫，此處顯式呼叫以確保
        # 「先落終態、再判 gate」的順序，讓 failed 旗標在同一個 lock 區間內即時可讀。
        session.expire_ready_reconfirm()
        if session.ready_reconfirm_failed:
            return {
                "ok": False,
                "error_code": ERROR_READY_UNCONFIRMED,
                "selector": session.profile.com,
                "hint": (
                    f"READY 已逾 {READY_RECONFIRM_MAX_S:.0f}s／"
                    f"{READY_RECONFIRM_MAX_ATTEMPTS} 次未能經 nonce probe 再確認"
                    "（可能卡在 bootloader）；請 session self-test 取分類後 interactive-open 處理"
                ),
                "recommended_action": "self_test",
            }
        if session.agent_gate_active():
            if session.boot_quiet_active():
                retry_after_s = round(
                    max(session.boot_quiet_until - time.monotonic(), 0.0), 1
                )
            else:
                retry_after_s = READY_RECONFIRM_RETRY_S
            return {
                "ok": False,
                "error_code": ERROR_AUTOBOOT_QUIET,
                "selector": session.profile.com,
                "retry_after_s": retry_after_s,
            }
        return None

    # --- bootloader 卡死偵測（#162 C 組）----------------------------------------
    @staticmethod
    def _bootloader_evidence(session: SessionRuntime, bridge: UARTBridge | None) -> str | None:
        """RX tail 尾行是否為 bootloader prompt；回傳命中的 pattern 或 ``None``。

        純讀取、無副作用。樣式來源優先 ``profile.bootloader_prompts``，缺配置時退回
        ``UBOOT_FALLBACK_PROMPTS``——線上 ``~/.config/serialwrap/profiles/*.yaml`` 的
        prpl-template 實測就漏了此欄位（出貨資產有），漂移下若不 fallback，整條
        bootloader 偵測都是 no-op，operator 拿不到任何可行動訊號。
        """
        if bridge is None:
            return None
        try:
            tail = clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))
        except Exception:
            return None
        patterns = session.profile.bootloader_prompts or UBOOT_FALLBACK_PROMPTS
        return _matches_any_bootloader_prompt(tail, patterns)

    # --- 帳密解析終態（#140）---------------------------------------------------
    @staticmethod
    def _credentials_declared_but_unresolved(
        profile: SessionProfile, auth_res: AuthResolution
    ) -> bool:
        """profile 宣告了帳密來源、但解析結果非 ok（帳密為空）。

        純函式：``not_configured``（未宣告帳密）因 ``user_env``/``pass_env``/``env_file``
        皆空而回 False，故未宣告帳密的 passwordless/auto-login 路徑完全不受影響。
        """
        declared = bool(profile.user_env or profile.pass_env or profile.env_file or profile.console_unlock_env)
        return declared and auth_res.reason != "ok"

    @staticmethod
    def _boot_quiet_blocks_probe(session: SessionRuntime, now: float | None = None) -> bool:
        """隱藏 lock 由 FSM 被動等待 RX 靜默；一般 profile 保留 180 秒 gate。"""
        return session.boot_quiet_active(now) and not session.profile.console_unlock_env

    def _begin_login_run_locked(
        self, session: SessionRuntime, bridge: UARTBridge | None = None
    ) -> TxPermit:
        """在既有 probe pre-check 的 manager lock 內建立新的登入世代 permit。"""
        session.login_gen += 1
        session._push_probe_gate(bridge)
        return TxPermit(
            f"system:login:{session.login_gen}",
            session.bridge_generation,
            "system_probe",
            login_gen=session.login_gen,
        )

    def _self_test_permit_locked(
        self, session: SessionRuntime, nonce: str
    ) -> TxPermit:
        """以 self-test pre-check 同一把 manager lock 擷取不可變提交身分。"""
        return TxPermit(
            f"system:self_test:{nonce}",
            session.bridge_generation,
            "self_test_probe",
        )

    @staticmethod
    def _system_permit_context(bridge: UARTBridge, permit: TxPermit) -> Any:
        setter = getattr(bridge, "system_permit", None)
        return setter(permit) if callable(setter) else contextlib.nullcontext()

    @staticmethod
    def _operation_permit_context(bridge: UARTBridge, permit: TxPermit) -> Any:
        setter = getattr(bridge, "operation_permit", None)
        return setter(permit) if callable(setter) else contextlib.nullcontext()

    @staticmethod
    def _send_bytes_with_permit(
        bridge: UARTBridge,
        payload: bytes,
        *,
        source: str,
        cmd_id: str | None,
        permit: TxPermit,
    ) -> Any:
        """在新舊 bridge 介面間傳遞 permit，保留固定簽章測試替身相容性。"""
        send_bytes = bridge.send_bytes
        try:
            parameters = inspect.signature(send_bytes).parameters
        except (TypeError, ValueError):
            accepts_permit = False
        else:
            accepts_permit = "permit" in parameters
        if accepts_permit:
            return send_bytes(payload, source=source, cmd_id=cmd_id, permit=permit)
        operation_context = getattr(bridge, "operation_permit", None)
        if callable(operation_context):
            with operation_context(permit):
                return send_bytes(payload, source=source, cmd_id=cmd_id)
        return send_bytes(payload, source=source, cmd_id=cmd_id)

    @staticmethod
    def _send_command_with_permit(
        bridge: UARTBridge,
        command: str,
        *,
        source: str,
        cmd_id: str | None,
        permit: TxPermit,
    ) -> Any:
        """在新舊 bridge 介面間傳遞 permit，保留固定簽章替身相容性。"""
        send_command = bridge.send_command
        try:
            parameters = inspect.signature(send_command).parameters
        except (TypeError, ValueError):
            accepts_permit = False
        else:
            accepts_permit = "permit" in parameters
        if accepts_permit:
            return send_command(
                command, source=source, cmd_id=cmd_id, permit=permit
            )
        operation_context = getattr(bridge, "operation_permit", None)
        if callable(operation_context):
            with operation_context(permit):
                return send_command(command, source=source, cmd_id=cmd_id)
        return send_command(command, source=source, cmd_id=cmd_id)

    def _emit_credentials_unresolved_warning(
        self, session_id: str, com: str, auth_res: AuthResolution
    ) -> None:
        """進入 CREDENTIALS_UNRESOLVED 終態時輸出一次 log + WAL 警告（#140）。

        以 ``session.credentials_warned`` 去重，避免 recovery 迴圈逐輪重複告警。
        訊息含 env_file 實際解析路徑與 reason，**絕不含帳密值**（WAL payload 為空）。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.credentials_warned:
                return
            session.credentials_warned = True
        import logging
        logging.getLogger("serialwrap").warning(
            "session %s 帳密解析未完成（reason=%s, env_file=%s）：不對 login prompt 送空帳密，"
            "標記 CREDENTIALS_UNRESOLVED 終態，請補齊帳密後手動 attach/recover",
            com, auth_res.reason, auth_res.env_file_path or "-",
        )
        try:
            self._wal.append(
                com=com,
                direction="META",
                source="system",
                payload=b"",
                meta={
                    "event": "credentials_unresolved",
                    "reason": auth_res.reason,
                    "env_file": auth_res.env_file_path or "",
                },
            )
        except Exception:
            # 稽核寫入為 best-effort，不得讓告警路徑拋出中斷 attach/recovery。
            pass

    # --- RX 指標防禦讀取與 probe 失敗精煉（#150 #153）---------------------------
    @staticmethod
    def _bridge_rx_bytes_last_10s(bridge: Any) -> int | None:
        """defensive 讀 bridge.rx_stats()['rx_bytes_last_10s']（#153）。

        測試 fake bridge 無此法／回傳非 dict（MagicMock）時一律回 None（視為不支援）。
        """
        fn = getattr(bridge, "rx_stats", None)
        if not callable(fn):
            return None
        try:
            stats = fn()
        except Exception:
            return None
        if not isinstance(stats, dict):
            return None
        value = stats.get("rx_bytes_last_10s")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    @staticmethod
    def _bridge_rx_total(bridge: Any) -> int | None:
        """defensive 讀 bridge.rx_total_bytes()（#150）；不支援時回 None。"""
        fn = getattr(bridge, "rx_total_bytes", None)
        if not callable(fn):
            return None
        try:
            value = fn()
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    def _refine_probe_failure(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        err: str | None,
        rx_before: int | None,
    ) -> tuple[str | None, str | None]:
        """probe 失敗碼的 transport 層精煉（#150，單一 choke point）。

        回傳 ``(err, detail)``：翻轉為 TRANSPORT_STALL 時 detail 帶 host 層復原提示，
        並做一次性 log + WAL META 告警（``transport_stall_warned`` 去重、RX 恢復重臂）；
        未翻轉但 ``err`` 屬 login FSM 失敗碼（``LOGIN_FSM_DETAIL_ERRORS``，#174）時，
        detail 帶失敗當下的 rx tail（截尾去控制碼），取代恆為 null 的現況；其餘原樣
        直通、detail=None。**只在真的送過 probe 後呼叫**——boot-quiet 合成的
        PROMPT_UNAVAILABLE（沒送 probe、且 quiet 代表剛有 RX）不得經此。不得在持有
        self._lock 時呼叫（內部會取鎖做去重）。
        """
        if err is None:
            return None, None
        rx_after = self._bridge_rx_total(bridge)
        if rx_before is None or rx_after is None:
            return err, None
        now = time.monotonic()
        refined, flipped = classify_probe_failure(
            err,
            rx_delta=rx_after - rx_before,
            last_rx_mono=session.last_rx_mono,
            now=now,
        )
        if not flipped:
            if err in LOGIN_FSM_DETAIL_ERRORS:
                return err, _login_fsm_rx_tail_detail(bridge)
            return err, None
        rx_age_s = now - session.last_rx_mono
        real_path = getattr(bridge, "device_path", None)
        detail = transport_stall_hint(real_path, rx_age_s)
        emit = False
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is not None and not current.transport_stall_warned:
                current.transport_stall_warned = True
                emit = True
        if emit:
            import logging
            logging.getLogger("serialwrap").warning(
                "session %s 疑似 transport stall（零 RX %.1fs、probe 全程無 echo，real_path=%s）：%s",
                session.profile.com, rx_age_s, real_path or "-", detail,
            )
            try:
                self._wal.append(
                    com=session.profile.com,
                    direction="META",
                    source="system",
                    payload=b"",
                    meta={
                        "event": "transport_stall_suspected",
                        "real_path": real_path or "",
                        "rx_age_s": round(rx_age_s, 1),
                    },
                )
            except Exception:
                # 稽核寫入為 best-effort，不得讓告警路徑拋出中斷 attach/recovery。
                pass
        return refined, detail

    def _is_reprobe_prompt_error(self, state: str, last_error: str | None) -> bool:
        # RX_FLOOD（#153）／TRANSPORT_STALL（#150）必須保留 reprobe 資格：
        # - RX_FLOOD：洪水中 _rx_idle_enough 天然退避，排空後 3s 內自動接手升 READY
        #   ——漏加會讓 flood session 永不自動重探、卡死 ATTACHED（比原病更糟）。
        # - TRANSPORT_STALL：probe 是無害 newline，誤判時可自癒（RX 一恢復，下次
        #   probe 見 echo → delta>0 → 回原分類或升 READY）。
        if not last_error:
            return False
        prompt_related = (
            last_error == "PROMPT_UNAVAILABLE"
            or last_error == "PROMPT_TIMEOUT"
            or last_error.endswith("_PROMPT_TIMEOUT")
            or last_error == ERROR_RX_FLOOD
            or last_error == ERROR_TRANSPORT_STALL
        )
        if state == "ATTACHED":
            return prompt_related
        if state == "DETACHED":
            return (
                last_error == "PROMPT_TIMEOUT"
                or last_error.endswith("_PROMPT_TIMEOUT")
                or last_error == ERROR_RX_FLOOD
                or last_error == ERROR_TRANSPORT_STALL
            )
        return False

    def _rx_idle_enough(self, session: SessionRuntime, now: float) -> bool:
        return session.last_rx_mono == 0.0 or (now - session.last_rx_mono) >= REPROBE_RX_IDLE_S

    def _human_active_locked(self, session: SessionRuntime, now: float) -> bool:
        if session.bridge is None or session.interactive_session_id is None:
            return False
        lease = self._interactive.get(session.interactive_session_id)
        if lease is None or not lease.owner.startswith("human:"):
            return False
        client_id = lease.owner.split(":", 1)[1]
        if not session.bridge.console_has_external_peer(client_id):
            return False
        snapshot = session.bridge.snapshot()
        if snapshot.get("interactive_owner") != lease.owner:
            return False
        last = snapshot.get("last_human_input_at")
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            return False
        return (now - last) <= HUMAN_ACTIVE_WINDOW_S

    def _record_reprobe_attempt_locked(self, session: SessionRuntime, now: float) -> None:
        session.reprobe_attempts += 1
        if session.reprobe_attempts >= REPROBE_MAX_ATTEMPTS:
            session.reprobe_exhausted = True
            session.next_reprobe_at = None
            return
        interval = min(
            REPROBE_BACKOFF_S * (2 ** max(session.reprobe_attempts - 1, 0)),
            REPROBE_MAX_INTERVAL_S,
        )
        session.next_reprobe_at = now + interval

    def _prepare_reprobe_locked(
        self, session: SessionRuntime, now: float
    ) -> tuple[str, SessionRuntime, UARTBridge | None, str] | None:
        by_id = session.profile.device_by_id
        if not by_id or by_id in self._released_by_ids or by_id not in self._devices:
            return None
        if session.state in {"RELEASED", "FLASHING", "ATTACHING", "RECOVERING"}:
            return None
        # passthrough 不執行 READY reconfirm 或 ATTACHED readiness probe。
        if session.profile.platform == "passthrough" and session.state in {"ATTACHED", "READY"}:
            return None
        # #162 有界化：排 probe 前先評估 pending 是否已逾時／逾次落終態——落定後
        # pending 為 False，下方 READY 分支自然不再排 probe，gate 也改回不可重試的
        # READY_UNCONFIRMED（見 _agent_gate_reject_locked）。
        if session.state == "READY":
            session.expire_ready_reconfirm(now)
        # #162 READY 再確認分支：名義 READY＋pending（quiet 已結束）才排確認 probe——
        # 該 probe 的 "\n"+nonce 順帶消耗 askconsole 啟用 banner，成功後 confirm_ready
        # 解除 agent gate。quiet 進行中維持 #130 TX 靜默（本函式下方 quiet gate 亦擋）。
        ready_reconfirm = (
            session.state == "READY"
            and session.ready_reconfirm_pending
            and not self._boot_quiet_blocks_probe(session, now)
        )
        if session.state == "READY" and not ready_reconfirm:
            return None
        # 帳密解析終態（#140）：CREDENTIALS_UNRESOLVED 為明確終態，不自動重探
        # （否則反覆對 login prompt 送空帳密）；需操作者補帳密後手動 attach/recover。
        # （_is_reprobe_prompt_error 已隱含排除此錯，這裡顯式早退以自我說明並防未來回歸。）
        if session.last_error == ERROR_CREDENTIALS_UNRESOLVED:
            return None
        if session.reprobe_exhausted or session.reprobe_attempts >= REPROBE_MAX_ATTEMPTS:
            session.reprobe_exhausted = True
            session.next_reprobe_at = None
            # #162 有界化（回歸修）：``reprobe_exhausted`` 是為 ATTACHED readiness 重探
            # 設計的 latch（第 10 次失敗後靜默停手，且只在 READY **轉移** 點才被
            # _reset_reprobe_progress_locked 解開）。一個「已經是 READY」的 session 不會
            # 再轉移 → latch 永遠解不開 → 確認 probe 永遠排不上 → pending 永久 True →
            # agent 命令永久 AUTOBOOT_QUIET。這是最短的無界路徑，必須改成落終態而非
            # 靜默凍結。
            if ready_reconfirm:
                session.expire_ready_reconfirm(now, force=True)
            return None
        if session.next_reprobe_at is not None and now < session.next_reprobe_at:
            return None
        # #130：boot quiet window 內不自動重探（probe 的 "\n" 會打斷 U-Boot autoboot）。
        # 不累加 attempts、不排 backoff——window 解除（RX 見 login/prompt）或過期後，
        # 下一個 tick 立即恢復既有 reprobe 流程。
        if self._boot_quiet_blocks_probe(session, now):
            return None
        # #162：READY 再確認分支不看 last_error（名義 READY 的 last_error 恆 None，
        # _is_reprobe_prompt_error 對 READY 必回 False）；其餘守衛（backoff／RX idle／
        # human-active／inflight）與 ATTACHED 分支完全共用。
        if not ready_reconfirm and not self._is_reprobe_prompt_error(session.state, session.last_error):
            return None
        if not self._rx_idle_enough(session, now):
            return None
        if self._human_active_locked(session, now):
            return None

        if session.state in ("ATTACHED", "READY"):
            if session.bridge is None:
                return None
            # 已有 auto worker 在跑這個 session → 不重複 spawn（single-flight，Finding 2）。
            if session.session_id in self._reprobe_inflight:
                return None
            interval = min(
                REPROBE_BACKOFF_S * (2 ** max(session.reprobe_attempts, 0)),
                REPROBE_MAX_INTERVAL_S,
            )
            session.next_reprobe_at = now + interval
            self._reprobe_inflight.add(session.session_id)
            return ("probe", session, session.bridge, by_id)

        if session.state == "DETACHED":
            session.state = "ATTACHING"
            session.last_error = None
            self._record_reprobe_attempt_locked(session, now)
            return ("attach", session, None, by_id)

        return None

    def _finish_probe_reprobe(self, session_id: str, bridge: UARTBridge, result: dict[str, Any], now: float) -> None:
        with self._lock:
            if self._handoff_frozen:
                # 交接凍結後，在途 probe 的結果不得再把 session 推進到 READY（零狀態改寫）。
                return
            current = self._sessions.get(session_id)
            if current is None or current.bridge is not bridge:
                return
            result_session = result.get("session") if isinstance(result, dict) else None
            result_state = result_session.get("state") if isinstance(result_session, dict) else current.state
            if current.state == "READY" or result_state == "READY":
                self._reset_reprobe_progress_locked(current)
                return
            self._record_reprobe_attempt_locked(current, now)
            # #162 有界化（A6）：確認 probe 每失敗一次即累加，逾 READY_RECONFIRM_MAX_ATTEMPTS
            # 落終態。pending 期間的失敗才算——非 pending 的 ATTACHED readiness 重探不計。
            if current.ready_reconfirm_pending:
                current.ready_reconfirm_attempts += 1
                current.expire_ready_reconfirm(now)
            # #162 C 組：probe 失敗且 RX tail 尾行是 bootloader prompt → 板子卡在
            # bootloader，繼續重探沒有意義（prpl-template 的 prompt_regex 在數學上不可能
            # 匹配 `=> `）。舊行為是「第 10 次靜默 exhausted、state/last_error 不變、不發
            # 事件、不做分類」——既不自復也不告訴任何人為什麼。改為**有理由的終態**：
            # last_error=BOOTLOADER_STUCK（self_test / recover 據此回 BOOTLOADER 分類與
            # recover_interactive 建議）、立即 exhausted、pending 直接落 READY_UNCONFIRMED。
            matched = self._bootloader_evidence(current, bridge)
            if matched is not None:
                current.last_error = ERROR_BOOTLOADER_STUCK
                current.last_error_detail = f"matched_prompt={matched}"
                current.reprobe_exhausted = True
                current.next_reprobe_at = None
                current.expire_ready_reconfirm(now, force=True)

    def _reprobe_probe_lock_locked(self, session_id: str) -> threading.Lock:
        """取得（必要時建立）per-session probe 互斥鎖；須在持有 self._lock 時呼叫。"""
        lock = self._reprobe_probe_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            self._reprobe_probe_locks[session_id] = lock
        return lock

    def _reprobe_target_still_valid_locked(
        self, session: SessionRuntime, bridge: UARTBridge, now: float
    ) -> bool:
        """auto worker 真正寫 probe 前的最終驗證（須持有 self._lock）。

        防止 job 收集後、probe 前狀態翻轉的競態（Finding 1）：例如 flash broker 搶進
        FLASHING、人類介入、device 被釋放/移除等。任一條件不再成立即放棄本次重探。
        """
        if session.bridge is not bridge:
            return False
        by_id = session.profile.device_by_id
        if not by_id or by_id in self._released_by_ids or by_id not in self._devices:
            return False
        if session.state not in ("ATTACHED", "READY"):
            return False
        if session.profile.platform == "passthrough":
            return False
        # #162：READY 需再確認 pending 才 valid（pending 已被其他確認點清掉＝
        # 不必再 probe）；_is_reprobe_prompt_error 檢查僅施於 ATTACHED。
        if session.state == "READY":
            # #162 有界化：寫 probe bytes 前先評估逾時／逾次終態（本函式是 READY
            # 分支唯一的「daemon 主動 TX」授權點——#130 之後 READY 原本是「daemon
            # 絕不主動寫」的，#162 新開了這條 TX 面，必須自帶終止條件）。
            session.expire_ready_reconfirm(now)
            if not session.ready_reconfirm_pending:
                return False
            # D1：quiet 過期後若 RX tail 顯示板子仍卡在 bootloader，就**不要**再對它
            # 送 "\n"+nonce——那是 U-Boot 下的可執行文字，且確認在數學上不可能成功
            # （prpl-template 的 prompt_regex 匹配不到 `=> `）。直接落 C 組終態。
            # （last_error 不動：名義 READY 的 last_error 恆為 None 是既有不變量，
            #   分類資訊由 self_test／recover 於當下重新從 RX tail 取得。）
            if self._bootloader_evidence(session, bridge) is not None:
                session.expire_ready_reconfirm(now, force=True)
                return False
        if session.reprobe_exhausted:
            return False
        if session.state == "ATTACHED" and not self._is_reprobe_prompt_error(session.state, session.last_error):
            return False
        # #130：job 收集後、worker 實際寫入前 boot banner 才抵達 → 最終驗證擋下，
        # 不得對 autoboot 倒數中的 UART 送 probe bytes。
        if self._boot_quiet_blocks_probe(session, now):
            return False
        if self._human_active_locked(session, now):
            return False
        return True

    def _spawn_reprobe_probe(self, session_id: str, bridge: UARTBridge) -> None:
        """在背景 worker 執行 ATTACHED 的 readiness probe，避免阻塞 DeviceWatcher tick（Finding 3）。"""
        with self._lock:
            probe_lock = self._reprobe_probe_lock_locked(session_id)

        def _run() -> None:
            acquired = probe_lock.acquire(blocking=False)
            try:
                if not acquired:
                    # manual attach/recover 正持有 probe 鎖 → 本次跳過，交給 backoff 後重試（Finding 2）。
                    return
                start = time.monotonic()
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is None or session.bridge is not bridge:
                        return
                    # 交接凍結後不再開始 readiness probe（SC-11，零 TX、零登入世代產生）。
                    if self._handoff_frozen:
                        return
                    if not self._reprobe_target_still_valid_locked(session, bridge, start):
                        return
                try:
                    result = self._probe_existing_bridge(session, bridge)
                except Exception as exc:
                    # backoff 以 probe「完成時間」計算，而非 start：probe 可能阻塞到 profile timeout
                    # （10/15s），用 start 會讓 next_reprobe_at 落在過去、下一 tick 立刻重探、
                    # 慢速/靜默開機時過早 exhausted（#69 Finding round6）。
                    done = time.monotonic()
                    with self._lock:
                        current = self._sessions.get(session_id)
                        if (
                            current is not None
                            and current.bridge is bridge
                            and current.state not in {"FLASHING", "RELEASED"}
                            and not self._handoff_frozen
                        ):
                            current.state = "ATTACHED"
                            current.last_error = f"REPROBE_FAILED:{type(exc).__name__}"
                            self._record_reprobe_attempt_locked(current, done)
                    return
                self._finish_probe_reprobe(session_id, bridge, result, time.monotonic())
            finally:
                if acquired:
                    probe_lock.release()
                with self._lock:
                    self._reprobe_inflight.discard(session_id)

        thread = threading.Thread(target=_run, name=f"serialwrap-reprobe-{session_id}", daemon=True)
        with self._lock:
            self._reprobe_workers = [t for t in self._reprobe_workers if t.is_alive()]
            self._reprobe_workers.append(thread)
        thread.start()

    def join_reprobe_workers(self, timeout: float | None = None) -> None:
        """等待目前所有 readiness probe worker 結束（供測試與優雅關閉使用）。"""
        with self._lock:
            workers = list(self._reprobe_workers)
        for thread in workers:
            thread.join(timeout)
        with self._lock:
            self._reprobe_workers = [t for t in self._reprobe_workers if t.is_alive()]

    def _expire_interactive_leases_tick(self, now: float | None = None) -> int:
        """由週期 tick 收回到期的非 human lease，並在鎖外執行還原與回放。"""
        tick_now = time.monotonic() if now is None else now
        post_actions: list[tuple[str, _PostCloseAction]] = []
        expired_count = 0
        with self._lock:
            # SC-11：交接凍結後 tick 不改動 lease（也不排還原／回放）；鎖內重新檢查，覆蓋頂部無鎖檢查之後的凍結。
            if self._handoff_frozen:
                return 0
            for interactive_id, lease in list(self._interactive.items()):
                if lease.status != "active" or lease.owner.startswith("human:"):
                    continue
                if tick_now - lease.last_activity_at <= lease.timeout_s:
                    continue
                session = self._sessions.get(lease.session_id)
                if session is None:
                    self._record_interactive_terminal_locked(
                        lease, terminal_status="expired", keep_tombstone=True,
                    )
                    expired_count += 1
                    continue
                closed, post = self._close_interactive_locked(
                    session,
                    interactive_id=interactive_id,
                    terminal_status="expired",
                    keep_tombstone=True,
                )
                if closed is not None:
                    if post.needs_resume and post.bridge is not None:
                        token = object()
                        self._interactive_post_close[session.session_id] = token
                        bridge_generation = session.bridge_generation
                        expected_interactive_id = session.interactive_session_id
                        post.should_execute = lambda s=session, b=post.bridge, g=bridge_generation, i=expected_interactive_id, t=token: self._interactive_tick_action_is_current(
                            s, b, g, i, t,
                        )
                        post.on_complete = lambda s=session, t=token: self._finish_interactive_tick_action(
                            s, t,
                        )
                    post_actions.append((interactive_id, post))
                    expired_count += 1
        for interactive_id, post in post_actions:
            try:
                post.execute()
            except Exception as exc:
                logging.getLogger("serialwrap").warning(
                    "serialwrap: interactive lease 到期後還原失敗（interactive_id=%s error=%s）",
                    interactive_id,
                    type(exc).__name__,
                )
        return expired_count

    def _interactive_tick_action_is_current(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        bridge_generation: int,
        interactive_id: str | None,
        token: object,
    ) -> bool:
        """確認鎖外到期收尾仍屬於原 session、bridge 與 lease ownership。"""
        with self._lock:
            return (
                self._interactive_post_close.get(session.session_id) is token
                and self._sessions.get(session.session_id) is session
                and session.bridge is bridge
                and session.bridge_generation == bridge_generation
                and session.interactive_session_id == interactive_id
            )

    def _finish_interactive_tick_action(self, session: SessionRuntime, token: object) -> None:
        """鎖外到期收尾完成後解除該 session 的 lease admission gate。"""
        with self._lock:
            if self._interactive_post_close.get(session.session_id) is token:
                self._interactive_post_close.pop(session.session_id, None)

    def reconcile_readiness(self) -> None:
        """週期性重探可復原的非 READY session。

        本方法由 DeviceWatcher tick 同步呼叫，因此只在 lock 內收集候選、不在此同步等待 probe；
        ATTACHED 的 readiness probe 改交背景 worker（Finding 3），DETACHED 走既有 threaded _spawn_attach。
        交接凍結（freeze_for_handoff）後整個 tick 不再改動 session 或送出 TX（SC-11，零 TX）。
        """
        if self._handoff_frozen:
            return
        self._expire_interactive_leases_tick()
        now = time.monotonic()
        jobs: list[tuple[str, str, UARTBridge | None, str]] = []
        promoted: list[str] = []
        with self._lock:
            # 頂部無鎖檢查之後、取得鎖之前，交接可能已完成凍結：鎖內重新檢查（SC-11，零狀態改寫）。
            if self._handoff_frozen:
                return
            promoted = self._promote_quiet_passthrough_locked(now)
            for session in list(self._sessions.values()):
                if self._handoff_frozen:
                    break
                prepared = self._prepare_reprobe_locked(session, now)
                if prepared is None:
                    continue
                action, prepared_session, bridge, by_id = prepared
                jobs.append((action, prepared_session.session_id, bridge, by_id))

        for session_id in promoted:
            self._on_ready(session_id)

        for action, session_id, bridge, by_id in jobs:
            if action == "attach":
                self._spawn_attach(by_id)
                continue
            if bridge is None:
                with self._lock:
                    self._reprobe_inflight.discard(session_id)
                continue
            self._spawn_reprobe_probe(session_id, bridge)

        # 週期回收孤兒 console + lease-backed 自癒重授（#76 / 症狀1 觸發C）。
        # 同一節流閘、同一 bridges 清單；snapshot / console_has_external_peer 在鎖外；
        # session 狀態突變（lease 記錄）在 self._lock 內。
        if now - self._last_console_reap_at >= _HUMAN_PEER_GRACE_S:
            self._last_console_reap_at = now
            with self._lock:
                # 交接凍結後 tick 不再回收或授予任何 console／lease（SC-11）。
                bridges = (
                    []
                    if self._handoff_frozen
                    else [s.bridge for s in self._sessions.values() if s.bridge is not None]
                )
            if bridges:
                # 取第一個 bridge 做全域 /proc 掃描（結果對所有 bridge 共用）；
                # procfs 不可用時回 None，各 bridge 的 reap 自行 fallback 至本身保守掃描（不誤剪）。
                held = bridges[0]._enumerate_all_held_paths()
                for bridge in bridges:
                    try:
                        bridge.reap_stale_consoles(held_slave_paths=held)
                    except Exception:  # noqa: BLE001 — 單一 bridge 回收失敗不得中斷 tick
                        pass

                # lease-backed 自癒：對每個無 owner / 無 agent / 無 flash 的 bridge，
                # 若 primary console 仍有外部 peer 但 session layer 已無 lease → 重授。
                # snapshot 在 self._lock 外（避免鎖內 I/O）；活 primary 判定複用 reaper 已算出的
                # 共享 held（不重掃 /proc）；session 狀態突變在 with self._lock 內；
                # try_grant_interactive_if_idle 原子。
                for bridge in bridges:
                    try:
                        snap = bridge.snapshot()
                        # 跳過條件：已有 owner、suspended owner、agent 進行中、flash 模式
                        if snap["interactive_owner"] is not None:
                            continue
                        if snap["suspended_owner"] is not None:
                            continue
                        if snap["agent_active"] or snap["flash_mode"]:
                            continue
                        primary_cid = snap["primary_client_id"]
                        if not primary_cid:
                            continue
                        # 活 primary 判定（鎖外）：複用 reaper 已算出的共享 held（snapshot 的 vtty
                        # 即 primary client 的 slave_path），避免每 bridge 在 _state_lock 內重掃 /proc。
                        # held 為 None（procfs 不可用）或 vtty 未知時，才退回 console_has_external_peer
                        # 做保守判定（沿用 _client_has_external_peer_locked 既有語意）。
                        if held is not None and snap["vtty"] is not None:
                            if snap["vtty"] not in held:
                                continue  # primary 的 slave 未被外部持有 → 非活 console
                        elif not bridge.console_has_external_peer(primary_cid):
                            continue  # procfs 不可用 / vtty 未知時的保守 fallback
                        with self._lock:
                            # 鎖外判定（snapshot／外部 peer）期間可能已完成交接凍結：鎖內重新檢查，凍結後不授予 lease。
                            if self._handoff_frozen:
                                continue
                            session = self._session_for_bridge_locked(bridge)
                            if session is None or session.state not in {"READY", "ATTACHED"}:
                                continue
                            if session.interactive_session_id is not None:
                                continue
                            # 原子 grant：僅當 bridge 仍完全 idle 才授予（消除 TOCTOU）
                            if bridge.try_grant_interactive_if_idle(f"human:{primary_cid}"):
                                # owner 已由 try_grant 原子設定；僅補 session-layer lease 記錄
                                self._record_self_heal_lease_locked(session, f"human:{primary_cid}")
                    except Exception:  # noqa: BLE001 — 單一 bridge 自癒失敗不得中斷 tick
                        pass

    def _session_for_bridge_locked(self, bridge: UARTBridge) -> SessionRuntime | None:
        """在 self._lock 內以 bridge 物件找對應的 SessionRuntime。"""
        for s in self._sessions.values():
            if s.bridge is bridge:
                return s
        return None

    def _record_self_heal_lease_locked(self, session: SessionRuntime, owner: str) -> None:
        """自癒 grant 後補 session-layer lease 記錄。

        比照 _open_interactive_locked，但不呼叫 bridge.set_interactive_owner()——
        owner 已由 try_grant_interactive_if_idle 原子設定，重設會破壞原子性。
        """
        interactive_id = uuid.uuid4().hex
        lease = InteractiveLease(
            interactive_id=interactive_id,
            session_id=session.session_id,
            owner=owner,
            created_at=now_iso(),
            timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
        )
        self._interactive[interactive_id] = lease
        session.interactive_session_id = interactive_id

    def set_alias_for_session(self, session_id: str, alias: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "session_id": session_id}
            session.profile = dataclasses.replace(session.profile, alias=alias)
            self._aliases.set_for_session(session_id, alias)
            self._save_state()
            return {"ok": True, "session_id": session_id, "alias": alias}

    def assign_alias(self, by_id: str, alias: str, profile: str | None = None) -> dict[str, Any]:
        self._aliases.assign_by_id(by_id, alias, profile)
        self._save_state()
        return {"ok": True, "alias": alias, "device_by_id": by_id}

    def unassign_alias(self, alias: str) -> dict[str, Any]:
        ok = self._aliases.unassign(alias)
        self._save_state()
        if not ok:
            return {"ok": False, "error_code": "ALIAS_NOT_FOUND", "alias": alias}
        return {"ok": True, "alias": alias}

    def _store_retained_consoles_locked(
        self,
        session: SessionRuntime,
        preserved: PreservedConsoles | None,
        *,
        human_owner: str | None = None,
        human_timeout_s: float | None = None,
    ) -> None:
        if not isinstance(preserved, PreservedConsoles) or not preserved.clients:
            session.retained_consoles = None
            session.retained_human_owner = None
            session.retained_human_timeout_s = None
            session.vtty_path = None
            return
        session.retained_consoles = preserved
        session.vtty_path = preserved.primary_vtty()
        session.retained_human_owner = None
        session.retained_human_timeout_s = None
        if human_owner is None or not human_owner.startswith("human:"):
            return
        client_id = human_owner.split(":", 1)[1]
        if client_id and preserved.has_client(client_id):
            session.retained_human_owner = human_owner
            session.retained_human_timeout_s = human_timeout_s

    def _restore_retained_human_console_locked(self, session: SessionRuntime) -> None:
        if session.session_id in self._interactive_post_close:
            return
        owner = session.retained_human_owner
        timeout_s = session.retained_human_timeout_s
        session.retained_human_owner = None
        session.retained_human_timeout_s = None
        if owner is None or session.bridge is None or not owner.startswith("human:"):
            return
        client_id = owner.split(":", 1)[1]
        if not client_id or not session.bridge.has_console(client_id):
            return
        lease, _ = self._refresh_interactive_locked(session)
        if lease is None:
            self._open_interactive_locked(
                session,
                owner=owner,
                timeout_s=timeout_s or max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
            )

    def _detach_session_locked(self, session: SessionRuntime, *, reason: str, drop_consoles: bool = False) -> None:
        preserved = session.retained_consoles
        retained_human_owner = session.retained_human_owner
        retained_human_timeout_s = session.retained_human_timeout_s
        if session.interactive_session_id is not None:
            lease = self._interactive.get(session.interactive_session_id)
            if lease is not None and lease.owner.startswith("human:"):
                retained_human_owner = lease.owner
                retained_human_timeout_s = lease.timeout_s
        if session.bridge is not None:
            preserved = session.bridge.stop(preserve_consoles=not drop_consoles)
            session.bridge = None
        if drop_consoles:
            preserved = None
            retained_human_owner = None
            retained_human_timeout_s = None
            session.retained_consoles = None
        self._store_retained_consoles_locked(
            session,
            preserved,
            human_owner=retained_human_owner,
            human_timeout_s=retained_human_timeout_s,
        )
        session.attached_real_path = None
        session.state = "DETACHED"
        session.detached_at = now_iso()
        session.last_error = reason
        self._reset_reprobe_progress_locked(session)
        if session.interactive_session_id is not None:
            lease = self._interactive.pop(session.interactive_session_id, None)
            if lease is not None:
                lease.status = "closed"
        session.interactive_session_id = None
        if self._admission is not None:
            self._admission.set_flag(session.profile.com, "lease", False)
            self._admission.set_flag(session.profile.com, "recovering", False)
        session._stashed_human_lease = None  # 清除 recovery lease stash，避免跨 bridge 殘留
        # 舊 operation callback 可能仍在 join timeout 外執行；切換 bridge epoch
        # 時先撤銷其 token，避免遲到 finally 觸碰新 session 的 busy 狀態。
        session._foreground_operation = None
        session.foreground_busy = False
        session.fg_cmd_started_mono = None
        session.fg_cmd_expected_duration_s = None
        self._stop_capture_locked(session)
        for cmd_id in list(session.background_cmd_ids):
            capture = self._background.get(cmd_id)
            if capture is not None:
                capture.status = "done"
        session.background_cmd_ids.clear()
        self._on_detached(session.session_id)

    def _begin_drain_locked(
        self,
        session: SessionRuntime,
        *,
        kind: str,
        reason: str,
        drop_consoles: bool,
        started_at: float,
        deadline: float | None = None,
        target_by_id: str | None = None,
        source: str = "cli",
        release_reason: str | None = None,
        bridge_override: "UARTBridge | None" = None,
    ) -> _DrainJob | None:
        """持 manager lock 建立去重 drain，先撤銷操作再凍結 bridge TX。"""
        if session._pending_drain is not None:
            return session._pending_drain
        bridge = session.bridge or bridge_override
        if bridge is None:
            return None
        by_id = session.profile.device_by_id
        job_deadline = deadline if deadline is not None else started_at + DRAIN_TIMEOUT_MS / 1000
        operation = session._foreground_operation
        job = _DrainJob(
            session_id=session.session_id,
            bridge=bridge,
            epoch=session.bridge_generation,
            kind=kind,
            reason=reason,
            drop_consoles=drop_consoles,
            aborted_cmd_id=operation.cmd_id if operation is not None else None,
            done=threading.Event(),
            deadline=job_deadline,
            target_by_id=target_by_id,
            source=source,
            release_reason=release_reason,
            aborted_foreground=session.foreground_busy,
            attach_started=self._attach_start_events.get(by_id)
            if by_id and self._attach_bridges.get(by_id) is bridge else None,
        )
        if job.aborted_cmd_id is not None:
            self._aborted_cmd_ids.add(job.aborted_cmd_id)
        session._pending_drain = job
        if by_id:
            self._draining_by_ids[by_id] = job
        session.drain_stalled = False
        if kind != "shutdown":
            session.state = "RECOVERING"
            session.last_error = reason
        session._stashed_human_lease = None
        if self._admission is not None:
            self._admission.set_flag(session.profile.com, "recovering", True)
        session._foreground_operation = None
        session.foreground_busy = False
        session.fg_cmd_started_mono = None
        session.fg_cmd_expected_duration_s = None
        if session.interactive_session_id is not None:
            lease = self._interactive.pop(session.interactive_session_id, None)
            if lease is not None:
                lease.status = "closed"
                if lease.owner.startswith("human:"):
                    session.retained_human_owner = lease.owner
                    session.retained_human_timeout_s = lease.timeout_s
        session.interactive_session_id = None
        if self._admission is not None:
            self._admission.set_flag(session.profile.com, "lease", False)
        self._stop_capture_locked(session)
        for cmd_id in list(session.background_cmd_ids):
            capture = self._background.get(cmd_id)
            if capture is not None:
                capture.status = "done"
        session.background_cmd_ids.clear()
        if kind == "release" and by_id:
            self._released_by_ids.add(by_id)
        if self._on_command_flush is not None:
            self._on_command_flush(
                session.session_id,
                "FLUSHED_BY_SHUTDOWN" if kind == "shutdown" else "FLUSHED_BY_RECOVERY",
            )
        bridge.begin_drain(
            job.epoch,
            preserve_consoles=not drop_consoles,
            deadline=job_deadline,
        )
        return job

    @staticmethod
    def _receipt_is_safe(receipt: Any) -> bool:
        return bool(
            getattr(receipt, "handle_closed", False)
            and getattr(receipt, "reader_stopped", False)
            and getattr(receipt, "writer_converged", False)
        )

    def _mark_drain_stalled(self, job: _DrainJob) -> None:
        with self._lock:
            session = self._sessions.get(job.session_id)
            if session is None or session._pending_drain is not job:
                return
            session.drain_stalled = True
            session.drain_timeout_epoch = job.epoch

    def _await_drain(self, job: _DrainJob) -> Any | None:
        remaining = max(0.0, job.deadline - time.monotonic())
        if job.attach_started is not None and not job.attach_started.wait(
            max(0.0, job.deadline - time.monotonic())
        ):
            self._mark_drain_stalled(job)
            threading.Thread(
                target=self._finish_drain_after_attach,
                args=(job,),
                name=f"serialwrap-drain-attach-{job.session_id}",
                daemon=True,
            ).start()
            return None
        receipt = job.bridge.finish_drain(
            remaining,
            on_late_receipt=lambda late: self._on_late_drain_receipt(job, late),
        )
        if receipt is None:
            self._mark_drain_stalled(job)
            return None
        if not self._receipt_is_safe(receipt):
            self._complete_drain(job, receipt)
            return None
        self._complete_drain(job, receipt)
        return receipt

    def _finish_drain_after_attach(self, job: _DrainJob) -> None:
        if job.attach_started is None:
            return
        job.attach_started.wait()
        self._await_drain(job)

    def _on_late_drain_receipt(self, job: _DrainJob, receipt: Any) -> None:
        try:
            if not self._receipt_is_safe(receipt):
                self._complete_drain(job, receipt)
                return
            self._complete_drain(job, receipt)
        except Exception as exc:  # noqa: BLE001 — 背景回呼不得逸出 thread
            if job.kind == "release":
                with self._lock:
                    session = self._sessions.get(job.session_id)
                    if session is not None:
                        session._release_persist_pending = True
                import logging
                logging.getLogger("serialwrap").error(
                    "serialwrap: release 持久化失敗（session=%s）：%s",
                    job.session_id,
                    exc,
                )

    def _complete_drain(self, job: _DrainJob, receipt: Any) -> None:
        should_attach = False
        with self._lock:
            session = self._sessions.get(job.session_id)
            if session is None or session._pending_drain is not job or job.done.is_set():
                return
            if not self._receipt_is_safe(receipt):
                session.drain_stalled = True
                session.drain_timeout_epoch = job.epoch
                job.receipt = receipt
                return
            job.receipt = receipt
            if session.bridge is job.bridge and session.bridge_generation == job.epoch:
                session.bridge = None
                session.attached_real_path = None
                session.vtty_path = None
            if session.attach_bridge is job.bridge:
                session.attach_bridge = None
            if job.drop_consoles:
                session.retained_consoles = None
                session.retained_human_owner = None
                session.retained_human_timeout_s = None
            else:
                self._store_retained_consoles_locked(
                    session,
                    getattr(receipt, "preserved", None),
                    human_owner=session.retained_human_owner,
                    human_timeout_s=session.retained_human_timeout_s,
                )
            session._pending_drain = None
            session.drain_stalled = False
            if self._admission is not None:
                self._admission.set_flag(session.profile.com, "recovering", False)
            by_id = session.profile.device_by_id
            if by_id and self._draining_by_ids.get(by_id) is job:
                self._draining_by_ids.pop(by_id, None)
            if job.kind == "release":
                session.state = "RELEASED"
                session.released_by = job.source
                session.released_at = session.released_at or now_iso()
                session.released_reason = job.release_reason
                # S1X-a-7：比照 0.3.2 release（_detach_session_locked）更新 detached_at 並重置 reprobe 進度。
                session.detached_at = now_iso()
                self._reset_reprobe_progress_locked(session)
                if by_id:
                    self._released_by_ids.add(by_id)
                session._release_persist_pending = True
                if self._attach_bridges.get(by_id or "") is job.bridge:
                    self._attach_bridges.pop(by_id or "", None)
                    self._attach_start_events.pop(by_id or "", None)
            elif job.kind == "shutdown":
                pass
            else:
                session.state = "DETACHED"
                session.detached_at = now_iso()
                if job.kind == "clear":
                    session.last_error = "CLEARED"
                elif job.kind == "bind":
                    session.last_error = "REBOUND"
                elif job.kind == "device_removed":
                    session.last_error = "DEVICE_REMOVED"
                elif job.kind == "io_error":
                    session.last_error = f"BRIDGE_DOWN:{job.reason.removeprefix('BRIDGE_DOWN:')}"
                elif job.kind == "recover":
                    session.last_error = "DEVICE_REBOUND_REQUIRED"
                self._reset_reprobe_progress_locked(session)
                if job.kind == "clear" and by_id and by_id in self._devices:
                    session.state = "ATTACHING"
                    # S1X-a-7：0.3.2 clear 在裝置在場時清掉 last_error（不留 CLEARED）。
                    session.last_error = None
                    should_attach = True
                elif job.kind == "bind":
                    target = job.target_by_id
                    if target:
                        if by_id:
                            self._released_by_ids.discard(by_id)
                        session.released_by = None
                        session.released_at = None
                        session.released_reason = None
                        session.profile = dataclasses.replace(session.profile, device_by_id=target)
                        self._binding_overrides[session.session_id] = target
                        has_device = target in self._devices
                        session.state = "ATTACHING" if has_device else "DETACHED"
                        session.last_error = None if has_device else "DEVICE_NOT_FOUND"
                        should_attach = has_device
                elif job.kind == "recover" and by_id and by_id in self._devices:
                    should_attach = True
                elif job.kind == "io_error" and by_id and by_id in self._devices:
                    if session.pending_auto_login:
                        session.state = "RECOVERING"
                    else:
                        session.state = "ATTACHING"
                        should_attach = True
                    session.last_error = None
            job.done.set()
        if job.kind == "release":
            self._persist_release_state(job.session_id)
        elif job.kind in {"bind", "clear"}:
            try:
                self._save_state()
            except Exception:
                raise
        if should_attach and by_id:
            self._spawn_attach(job.target_by_id if job.kind == "bind" and job.target_by_id else by_id)

    def _persist_release_state(self, session_id: str) -> None:
        """以單一持久化工作者完成 release；重複呼叫等待或接手失敗重試。"""
        while True:
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None or not session._release_persist_pending:
                    return
                event = self._release_persist_events.get(session_id)
                if event is None:
                    event = threading.Event()
                    self._release_persist_events[session_id] = event
                    owner = True
                else:
                    owner = False
            if not owner:
                event.wait()
                continue
            try:
                _save_release_state(self)
            except Exception:
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is not None:
                        session._release_persist_pending = True
                    self._release_persist_events.pop(session_id, None)
                    event.set()
                raise
            with self._lock:
                session = self._sessions.get(session_id)
                if session is not None:
                    session._release_persist_pending = False
                self._release_persist_events.pop(session_id, None)
                event.set()
            return

    def clear_session(self, selector: str) -> dict[str, Any]:
        started_at = time.monotonic()
        job: _DrainJob | None = None
        detached_in_lock = False
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            # FLASHING 或 flash probe 臨界區不得 detach bridge——否則會在 MCU 燒錄/偵測途中切斷 transport、
            # 寫壞韌體或汙染 probe（#69 r5；#83 RACE-2 final）。與 cmd submit / interactive 既有 FLASHING_BUSY
            # 守法一致；須先 exit_flashing 或待 probe 結束。
            if self._flash_busy_locked(session):
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com, "session": session.to_public_dict()}
            if session._pending_drain is not None:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            if session.state == "RELEASED" or session.profile.device_by_id in self._released_by_ids:
                return {"ok": True, "released": True, "session": session.to_public_dict()}
            by_id = session.profile.device_by_id
            if self._shutting_down:
                raise RuntimeError("daemon stopping")
            if session.bridge is not None:
                job = self._begin_drain_locked(
                    session,
                    kind="clear",
                    reason="CLEARED",
                    drop_consoles=False,
                    started_at=started_at,
                )
            else:
                self._detach_session_locked(session, reason="CLEARED", drop_consoles=False)
                detached_in_lock = True
                has_device = bool(by_id and by_id in self._devices)
                session.state = "ATTACHING" if has_device else "DETACHED"
                session.last_error = None if has_device else "CLEARED"
                self._reset_reprobe_progress_locked(session)
        if job is not None:
            self._on_detached(session.session_id)
            receipt = self._await_drain(job)
            if receipt is None:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": True,
                    "drain_timeout_epoch": job.epoch,
                    "session": session.to_public_dict(),
                }
        else:
            if not detached_in_lock:
                self._on_detached(session.session_id)
            self._save_state()
            if by_id and session.state == "ATTACHING":
                self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def release_device(self, selector: str, *, source: str = "cli", reason: str | None = None) -> dict[str, Any]:
        started_at = time.monotonic()
        release_deadline = started_at + DRAIN_TIMEOUT_MS / 1000
        job: _DrainJob | None = None
        attach_op: _AttachOp | None = None
        finish_unconsumed_op = False
        persist_retry = False
        detach_notified = False
        already_released = False
        closed_consoles = 0
        aborted_cmd = False
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            # FLASHING 或 flash probe 臨界區不得 release/detach——避免切斷進行中的燒錄/偵測 transport
            # （#69 r5；#83 RACE-2 final）。須先 exit_flashing 或待 probe 結束。
            if self._flash_busy_locked(session):
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com, "session": session.to_public_dict()}

            by_id = session.profile.device_by_id
            attach_op = (
                self._active_attach_ops.get(by_id or "")
                or self._attach_ops.get(by_id or "")
            )
            if attach_op is None and by_id:
                attach_op = self._preopened_ports.pop(by_id, None)
                finish_unconsumed_op = attach_op is not None

            if attach_op is not None:
                already_released = session.state == "RELEASED"
                if self._shutting_down and not already_released:
                    raise RuntimeError("daemon stopping")
                first_release_request = attach_op.release_request is None
                if first_release_request:
                    release_source = (session.released_by or source) if already_released else source
                    release_reason = session.released_reason if already_released else reason
                    attach_op.release_request = (release_source, release_reason)
                    attach_op.release_deadline = release_deadline
                else:
                    release_deadline = attach_op.release_deadline or release_deadline
                attach_op.cancelled.set()
                if attach_op.probe_bridge is not None:
                    attach_op.probe_bridge.begin_drain(
                        attach_op.bridge_generation,
                        preserve_consoles=False,
                        deadline=release_deadline,
                    )
                if not already_released:
                    session.state = "RECOVERING"
                    if first_release_request:
                        session.drain_stalled = False
                        session.drain_timeout_epoch = None
                    if self._admission is not None:
                        self._admission.set_flag(session.profile.com, "recovering", True)
            elif session.state == "RELEASED":
                persist_retry = session._release_persist_pending
                already_released = True
            else:
                if self._shutting_down:
                    raise RuntimeError("daemon stopping")
                if session._pending_drain is not None:
                    if session._pending_drain.kind != "release":
                        return {
                            "ok": False,
                            "error_code": "SESSION_RECOVERING",
                            "drain_stalled": session.drain_stalled,
                            "drain_timeout_epoch": session.drain_timeout_epoch,
                            "session": session.to_public_dict(),
                        }
                    job = session._pending_drain
                    closed_consoles = job.closed_consoles
                    aborted_cmd = job.aborted_foreground
                else:
                    attach_bridge = self._attach_bridges.get(by_id or "")
                    active_bridge = session.bridge or attach_bridge
                    closed_consoles = len(active_bridge.list_consoles()) if session.bridge is not None else 0
                    aborted_cmd = session.foreground_busy
                    if active_bridge is not None:
                        job = self._begin_drain_locked(
                            session,
                            kind="release",
                            reason="RELEASED",
                            drop_consoles=True,
                            started_at=started_at,
                            source=source,
                            release_reason=reason,
                            bridge_override=attach_bridge if session.bridge is None else None,
                        )
                        if job is not None:
                            job.closed_consoles = closed_consoles
                            detach_notified = True
                    else:
                        self._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
                        session.state = "RELEASED"
                        session.released_by = source
                        session.released_at = now_iso()
                        session.released_reason = reason
                        session.drain_stalled = False
                        session._release_persist_pending = True
                        if by_id:
                            self._released_by_ids.add(by_id)
                        persist_retry = True

        if attach_op is not None:
            if finish_unconsumed_op:
                self._finish_attach_op(attach_op)
            else:
                attach_op.done.wait(max(0.0, release_deadline - time.monotonic()))
            if not attach_op.done.is_set():
                with self._lock:
                    session.drain_stalled = True
                    session.drain_timeout_epoch = attach_op.bridge_generation
                    if session.state != "RELEASED":
                        session.state = "RECOVERING"
                return {
                    "ok": False,
                    "error_code": ERROR_DRAIN_STALLED,
                    "drain_stalled": True,
                    "drain_timeout_epoch": attach_op.bridge_generation,
                    "session": session.to_public_dict(),
                }
            if session._release_persist_pending:
                self._persist_release_state(session.session_id)
            if already_released:
                return {"ok": True, "already_released": True, "session": session.to_public_dict()}
            return {
                "ok": True,
                "session": session.to_public_dict(),
                "closed_consoles": 0,
                "aborted_cmd": False,
            }
        if already_released:
            if persist_retry:
                self._persist_release_state(session.session_id)
            return {"ok": True, "already_released": True, "session": session.to_public_dict()}
        if job is not None:
            if detach_notified:
                self._on_detached(session.session_id)
            try:
                receipt = self._await_drain(job)
            except Exception:
                with self._lock:
                    session._release_persist_pending = True
                raise
            if receipt is None:
                return {
                    "ok": False,
                    "error_code": ERROR_DRAIN_STALLED,
                    "drain_stalled": True,
                    "drain_timeout_epoch": job.epoch,
                    "session": session.to_public_dict(),
                }
            return {
                "ok": True,
                "session": session.to_public_dict(),
                "closed_consoles": job.closed_consoles,
                "aborted_cmd": job.aborted_foreground,
            }
        self._persist_release_state(session.session_id)
        return {"ok": True, "session": session.to_public_dict(), "closed_consoles": closed_consoles, "aborted_cmd": aborted_cmd}

    def mark_flash_critical(self, com: str) -> None:
        """標記 COM 進入 flash 偵測 probe 臨界區（#83 RACE-2 final）：destructive op 一律 FLASHING_BUSY。"""
        with self._lock:
            self._flash_critical_coms.add(com)
            if self._admission is not None:
                self._admission.set_flag(com, "flashing", True)

    def unmark_flash_critical(self, com: str) -> None:
        """解除 flash probe 臨界區標記。"""
        with self._lock:
            self._flash_critical_coms.discard(com)
            if self._admission is not None:
                flashing = any(s.profile.com == com and s.state == "FLASHING" for s in self._sessions.values())
                self._admission.set_flag(com, "flashing", flashing)

    def _flash_busy_locked(self, session: "SessionRuntime") -> bool:
        """session 是否處於 flash 臨界區（FLASHING 狀態或 probe 進行中）——須在 self._lock 內呼叫。"""
        return session.state == "FLASHING" or session.profile.com in self._flash_critical_coms

    def collect_flash_candidates_and_mark(self) -> list[dict[str, Any]]:
        """**原子**收集 flash 偵測候選（非 command_capable、READY/ATTACHED）並同時標記 flash-critical。

        snapshot 與 mark 必須在同一把鎖內（#83 RACE-2 final）：否則「收集候選 → 釋鎖 → 逐一標記」之間，
        並發的 clear/release/bind 可在標記生效前 detach/rebind bridge（probe 隨後撞到 stale by_id→com 或
        新 bridge）。回傳候選 dict 串列（com/by_id/real_path/command_capable），呼叫端於偵測結束後對各
        com `unmark_flash_critical`。標記後 bind 被擋，故 probe 期間候選的 device_by_id 不會變。
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            for session in self._sessions.values():
                if session.profile.command_capable:
                    continue
                if session.state not in ("READY", "ATTACHED"):
                    continue
                by_id = session.profile.device_by_id
                com = session.profile.com
                if (
                    session._pending_drain is not None
                    or (by_id is not None and by_id in self._draining_by_ids)
                    or session.foreground_busy
                    or session._foreground_operation is not None
                ):
                    continue
                dev = self._devices.get(by_id) if by_id else None
                out.append({
                    "com": com,
                    "by_id": by_id,
                    "real_path": dev.real_path if dev is not None else None,
                    "command_capable": False,
                })
                if com:
                    self._flash_critical_coms.add(com)
                    if self._admission is not None:
                        self._admission.set_flag(com, "flashing", True)
        return out

    def enter_flashing(self, selector: str) -> dict:
        """進入 FLASHING：只標狀態 + 擋命令，**不** detach bridge（daemon 仍是 real device 唯一 reader）。"""
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state == "FLASHING":
                return {"ok": True, "already_flashing": True, "session": session.to_public_dict()}
            if session.state == "RELEASED":
                return {
                    "ok": False,
                    "error_code": "SESSION_NOT_READY",
                    "session": session.to_public_dict(),
                }
            by_id = session.profile.device_by_id
            if (
                session.state == "RECOVERING"
                or session._pending_drain is not None
                or (by_id is not None and by_id in self._draining_by_ids)
            ):
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "session": session.to_public_dict(),
                }
            session.flash_prev_state = session.state   # 記住以便結束後恢復
            session.state = "FLASHING"
            if self._admission is not None:
                self._admission.set_flag(session.profile.com, "flashing", True)
            if session.bridge is not None:
                session.bridge.set_flash_mode(True)   # 擋 console 注入（C2）
                invalidate_deferred = getattr(session.bridge, "invalidate_deferred", None)
                if callable(invalidate_deferred):
                    invalidate_deferred("flash")
            public = session.to_public_dict()
        return {"ok": True, "session": public}

    def exit_flashing(self, selector: str) -> dict:
        """結束 FLASHING：恢復先前狀態（bridge 全程未關，無需 re-attach）。"""
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            # 防禦縱深（#69 r2/r4）：不論目前 state 為何，只要有 bridge 就無條件解除 flash 模式。
            # 即使某競態路徑（command timeout recovery / probe）已把 state 搶改出 FLASHING，
            # 也確保 bridge 不會永久卡在 flash 模式、靜默丟棄所有非 flash 寫入。
            if session.bridge is not None:
                session.bridge.set_flash_mode(False)
            if session.state != "FLASHING":
                session.flash_prev_state = None
                if self._admission is not None:
                    self._admission.set_flag(
                        session.profile.com,
                        "flashing",
                        session.profile.com in self._flash_critical_coms,
                    )
                return {"ok": True, "not_flashing": True, "session": session.to_public_dict()}
            session.state = session.flash_prev_state or ("READY" if session.bridge is not None else "DETACHED")
            session.flash_prev_state = None
            if self._admission is not None:
                self._admission.set_flag(
                    session.profile.com,
                    "flashing",
                    session.profile.com in self._flash_critical_coms,
                )
            public = session.to_public_dict()
        return {"ok": True, "session": public}

    def _probe_external_holder(self, real_path: str, *, _proc_root: str = "/proc") -> dict[str, Any]:
        """唯讀偵測 real_path 的 holder；掃描失敗另回 holder_unknown，不推導安全。"""
        report = serial_port_mod.probe_holders(
            real_path,
            proc_root=_proc_root,
            exclude_pid=os.getpid(),
        )
        pids = list(report.pids)
        return {
            "pids": pids,
            "holder": pids[0] if pids else None,
            "holder_unknown": report.holder_unknown,
        }

    def _attach_inflight_draining_locked(self, session: SessionRuntime, by_id: str) -> bool:
        """attach 在途表命中時，判斷是否屬 drain／release 進行中（需持 _lock）。

        只有 pending drain、by_id 正在 drain、在途 op 帶 release 請求或已取消、或 session
        已 RELEASED 時才算；否則只是 probe／login 進行中的一般 attach（S1X-a-3）。
        """
        if session._pending_drain is not None or by_id in self._draining_by_ids:
            return True
        if session.state == "RELEASED" or by_id in self._released_by_ids:
            return True
        for table in (self._attach_ops, self._preopened_ports, self._active_attach_ops):
            op = table.get(by_id)
            if op is not None and (op.release_request is not None or op.cancelled.is_set()):
                return True
        return False

    def attach_device(self, selector: str, *, force: bool = False) -> dict[str, Any]:
        if self._shutting_down:
            raise RuntimeError("daemon stopping")
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            if self._shutting_down:
                raise RuntimeError("daemon stopping")
            if session._pending_drain is not None or (by_id and by_id in self._draining_by_ids):
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            # 冪等：session 已 attached（有 bridge）且非 RELEASED → 直接回覆，不要改 state。
            # 否則會被設成 ATTACHING，但 _attach_by_id 因 bridge 已存在而早退，卡在 ATTACHING。
            if session.bridge is not None and session.state != "RELEASED" and by_id not in self._released_by_ids:
                return {"ok": True, "already_attached": True, "session": session.to_public_dict()}
            if not by_id or by_id not in self._devices:
                return {"ok": False, "error_code": "DEVICE_NOT_PRESENT", "selector": selector, "device_by_id": by_id}
            real_path = self._devices[by_id].real_path
            session_id = session.session_id
            bridge_generation = session.bridge_generation
            profile = session.profile
            if (
                by_id in self._attach_ops
                or by_id in self._preopened_ports
                or by_id in self._active_attach_ops
                or by_id in self._attach_bridges
            ):
                # S1X-a-3：單純 attach 進行中（probe／login）回 ok＋session（0.3.2 行為）；
                # SESSION_RECOVERING 只保留給 drain／release 進行中（SC-04）。
                if not self._attach_inflight_draining_locked(session, by_id):
                    return {"ok": True, "session": session.to_public_dict()}
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
        if not force:
            holder = self._probe_external_holder(real_path)
            if holder["pids"]:
                return {
                    "ok": False,
                    "error_code": "DEVICE_STILL_HELD",
                    "pids": holder["pids"],
                    "holder_unknown": False,
                    "selector": selector,
                }

        with self._lock:
            if self._shutting_down:
                raise RuntimeError("daemon stopping")
            if self._attach_ops.get(by_id) is not None or self._preopened_ports.get(by_id) is not None:
                if not self._attach_inflight_draining_locked(session, by_id):
                    return {"ok": True, "session": session.to_public_dict()}
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            if session._pending_drain is not None or by_id in self._draining_by_ids:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            if self._sessions.get(session_id) is not session:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.profile.device_by_id != by_id or session.bridge_generation != bridge_generation:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            device = self._devices.get(by_id)
            if device is None or device.real_path != real_path:
                return {
                    "ok": False,
                    "error_code": "DEVICE_NOT_PRESENT",
                    "selector": selector,
                    "device_by_id": by_id,
                }
            op = _AttachOp(
                token=object(),
                session_id=session_id,
                by_id=by_id,
                bridge_generation=bridge_generation,
                real_path=real_path,
            )
            self._attach_ops[by_id] = op

        port: SerialPort | None = None
        try:
            port = uart_io_mod.open_serial_port(real_path, profile.uart)
            with op.port_lock:
                op.port = port
            port.open()
        except serial_port_mod.DeviceHeldError as exc:
            self._finish_attach_op(op)
            pids = list(exc.holder.pids)
            return {
                "ok": False,
                "error_code": "DEVICE_STILL_HELD",
                "pids": pids,
                "holder_unknown": False if pids else True,
                "selector": selector,
            }
        except (serial_port_mod.DeviceNotPresentError, FileNotFoundError):
            self._finish_attach_op(op)
            return {
                "ok": False,
                "error_code": "DEVICE_NOT_PRESENT",
                "selector": selector,
                "device_by_id": by_id,
            }
        except OSError as exc:
            placeholder_handoff = False
            with self._lock:
                cancelled = op.cancelled.is_set() or op.release_request is not None
                if not cancelled and self._attach_ops.get(by_id) is op:
                    self._attach_ops.pop(by_id, None)
                    if self._sessions.get(session_id) is session:
                        session.state = "DETACHED"
                        session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                        session.detached_at = now_iso()
                        self._released_by_ids.discard(by_id)
                        self._loaded_released.pop(session_id, None)
                        session.released_by = None
                        session.released_at = None
                        session.released_reason = None
                        if session.released_placeholder:
                            # OD-63：比照成功分支交接——佔位移除自身、保留原 COM，交 dynamic
                            # 偵測重新開埠；仍失敗時由 _record_dynamic_open_failure 在原 COM
                            # 留下可再 attach 的 DETACHED 列，不讓裝置成為孤兒。
                            self._retire_released_placeholder_locked(session)
                            self._pending_com[by_id] = session.profile.com
                            placeholder_handoff = True
                        public = session.to_public_dict()
                    else:
                        public = session.to_public_dict()
                else:
                    public = session.to_public_dict()
            self._finish_attach_op(op)
            if not cancelled:
                self._save_state()
                if placeholder_handoff:
                    self._spawn_attach(by_id, dynamic_only=True)
                return {"ok": True, "session": public}
            return {
                "ok": False,
                "error_code": "SESSION_RECOVERING",
                "drain_stalled": session.drain_stalled,
                "drain_timeout_epoch": session.drain_timeout_epoch,
                "session": session.to_public_dict(),
            }
        except BaseException:
            self._finish_attach_op(op)
            raise

        assert port is not None
        invalid_reason: str | None = None
        dynamic_handoff = False
        with self._lock:
            current_device = self._devices.get(by_id)
            if current_device is None or current_device.real_path != real_path:
                invalid_reason = "device"
            elif self._sessions.get(session_id) is not session:
                invalid_reason = "session"
            elif op.cancelled.is_set() or self._attach_ops.get(by_id) is not op:
                invalid_reason = "operation"
            elif session.profile.device_by_id != by_id or session.bridge_generation != bridge_generation:
                invalid_reason = "session_changed"
            elif session._pending_drain is not None or by_id in self._draining_by_ids:
                invalid_reason = "drain"
            elif self._shutting_down:
                invalid_reason = "shutdown"

            if invalid_reason is None:
                op.port = port
                self._preopened_ports[by_id] = op
                self._attach_ops.pop(by_id, None)
                self._released_by_ids.discard(by_id)
                self._loaded_released.pop(session_id, None)
                session.released_by = None
                session.released_at = None
                session.released_reason = None
                session.state = "ATTACHING"
                session.last_error = None
                self._reset_reprobe_progress_locked(session)
                public = session.to_public_dict()
                if session.released_placeholder:
                    # OD-63：佔位先移除自身並解除 by_id，已取得的 UART 交 template 偵測
                    # 建立正式 session（保留原 COM）；不以佔位的 passthrough profile attach。
                    self._retire_released_placeholder_locked(session)
                    self._pending_com[by_id] = session.profile.com
                    op.session_id = f"dynamic:{by_id}"
                    dynamic_handoff = True
            else:
                if self._attach_ops.get(by_id) is op:
                    self._attach_ops.pop(by_id, None)

        if invalid_reason is not None:
            self._finish_attach_op(op)
            if invalid_reason == "device":
                return {
                    "ok": False,
                    "error_code": "DEVICE_NOT_PRESENT",
                    "selector": selector,
                    "device_by_id": by_id,
                }
            if invalid_reason == "session":
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            return {
                "ok": False,
                "error_code": "SESSION_RECOVERING",
                "drain_stalled": session.drain_stalled,
                "drain_timeout_epoch": session.drain_timeout_epoch,
                "session": session.to_public_dict(),
            }

        self._save_state()
        if dynamic_handoff:
            self._spawn_attach(by_id, dynamic_only=True)
        else:
            self._spawn_attach(by_id)
        return {"ok": True, "session": public}

    def _retire_released_placeholder_locked(self, session: SessionRuntime) -> None:
        """OD-63：移除 RELEASED 佔位 session 與其持久化阻擋（須持 self._lock）。"""
        sid = session.session_id
        if self._sessions.get(sid) is session:
            del self._sessions[sid]
        self._loaded_released.pop(sid, None)
        self._binding_overrides.pop(sid, None)
        by_id = session.profile.device_by_id
        if by_id:
            self._released_by_ids.discard(by_id)
        for name, row in self._aliases.dump().items():
            if row.get("session_id") == sid:
                self._aliases.unassign(name)
        session.released_placeholder = False

    def bind_session(self, selector: str, device_by_id: str) -> dict[str, Any]:
        device_by_id = device_by_id.strip()
        if not device_by_id:
            return {"ok": False, "error_code": "INVALID_ARGS"}

        pending_attach: _AttachOp | None = None
        pending_attach_unconsumed = False
        with self._lock:
            current = self.get_session(selector)
            if current is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if current.released_placeholder:
                # OD-63：佔位只能經 device attach 解除後交 template 偵測；比照 attach_session 的
                # RELEASED 早退，不解除阻擋、不寫 override、不取消在途 attach、不 spawn。
                return {
                    "ok": True,
                    "released": True,
                    "recommended_action": "device_attach",
                    "session": current.to_public_dict(),
                }
            old_by_id = current.profile.device_by_id
            pending_attach = (
                self._active_attach_ops.get(old_by_id or "")
                or self._attach_ops.get(old_by_id or "")
            )
            if pending_attach is None and old_by_id:
                pending_attach = self._preopened_ports.pop(old_by_id, None)
                pending_attach_unconsumed = pending_attach is not None
            if pending_attach is not None:
                pending_attach.cancelled.set()
                pending_attach.bind_requested = True
                if pending_attach.probe_bridge is not None:
                    pending_attach.probe_bridge.begin_drain(
                        pending_attach.bridge_generation,
                        preserve_consoles=True,
                        deadline=time.monotonic() + DRAIN_TIMEOUT_MS / 1000,
                    )
        if pending_attach is not None:
            if pending_attach_unconsumed:
                completed = self._finish_attach_op(pending_attach)
            else:
                completed = pending_attach.done.wait(DRAIN_TIMEOUT_MS / 1000)
            if not completed:
                with self._lock:
                    current = self._sessions.get(pending_attach.session_id)
                    if current is not None:
                        current.state = "RECOVERING"
                        current.drain_stalled = True
                        current.drain_timeout_epoch = pending_attach.bridge_generation
                        return {
                            "ok": False,
                            "error_code": "SESSION_RECOVERING",
                            "drain_stalled": True,
                            "drain_timeout_epoch": pending_attach.bridge_generation,
                            "session": current.to_public_dict(),
                        }
            else:
                with self._lock:
                    worker = self._attach_threads.get(pending_attach.by_id)
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=0.1)
            with self._lock:
                if pending_attach.release_request is not None:
                    current = self._sessions.get(pending_attach.session_id)
                    if current is not None:
                        return {
                            "ok": False,
                            "error_code": "SESSION_RECOVERING",
                            "drain_stalled": current.drain_stalled,
                            "drain_timeout_epoch": current.drain_timeout_epoch,
                            "session": current.to_public_dict(),
                        }
            return self.bind_session(selector, device_by_id)

        started_at = time.monotonic()
        job: _DrainJob | None = None
        unconsumed_port: SerialPort | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if self._shutting_down:
                raise RuntimeError("daemon stopping")
            # FLASHING 或 flash probe 臨界區不得 rebind——rebind 會 detach 舊 bridge、切斷燒錄/偵測 transport
            # （#69 r5；#83 RACE-2 final）。須先 exit_flashing 或待 probe 結束。
            if self._flash_busy_locked(session):
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com, "session": session.to_public_dict()}
            for other in self._sessions.values():
                if other.session_id != session.session_id and other.profile.device_by_id == device_by_id:
                    return {
                        "ok": False,
                        "error_code": "DEVICE_ALREADY_BOUND",
                        "device_by_id": device_by_id,
                        "session_id": other.session_id,
                    }
            # 冪等：已綁定同 device 且 bridge 存在且 READY/ATTACHED → 不 detach
            if (
                session.bridge is not None
                and session.profile.device_by_id == device_by_id
                and session.state in ("READY", "ATTACHED")
            ):
                return {"ok": True, "already_bound": True, "session": session.to_public_dict()}
            if session._pending_drain is not None:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            if session.bridge is not None:
                job = self._begin_drain_locked(
                    session,
                    kind="bind",
                    reason="REBOUND",
                    drop_consoles=False,
                    started_at=started_at,
                    target_by_id=device_by_id,
                )
            else:
                old_by_id = session.profile.device_by_id
                if old_by_id:
                    self._attach_ops.pop(old_by_id, None)
                    pending_op = self._preopened_ports.pop(old_by_id, None)
                    if pending_op is not None:
                        unconsumed_port = pending_op.port
                if session.state == "RELEASED" and old_by_id:
                    self._released_by_ids.discard(old_by_id)
                session.released_by = None
                session.released_at = None
                session.released_reason = None
                session.profile = dataclasses.replace(session.profile, device_by_id=device_by_id)
                self._reset_reprobe_progress_locked(session)
                self._binding_overrides[session.session_id] = device_by_id
                has_device = device_by_id in self._devices
                session.state = "ATTACHING" if has_device else "DETACHED"
                session.last_error = None if has_device else "DEVICE_NOT_FOUND"

        if unconsumed_port is not None:
            try:
                unconsumed_port.close()
            except Exception:
                pass
        if job is not None:
            self._on_detached(session.session_id)
            receipt = self._await_drain(job)
            if receipt is None:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": True,
                    "drain_timeout_epoch": job.epoch,
                    "session": session.to_public_dict(),
                }
            return {"ok": True, "session": session.to_public_dict()}
        self._save_state()
        if has_device:
            self._spawn_attach(device_by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def _resolve_device_key(self, selector: str) -> tuple[str | None, SessionRuntime | None]:
        """解析 selector → (device_key, session)。selector 可為 COM/alias/sid（→ 既有 session 的
        device_by_id）或直接 by-id/by-path（→ 該字串即 device_key，session 可能為 None）。"""
        with self._lock:
            for sid, s in self._sessions.items():
                if selector in (sid, s.profile.com, s.profile.alias):
                    return s.profile.device_by_id, s
            for sid, s in self._sessions.items():
                if s.profile.device_by_id == selector:
                    return selector, s
            if selector in self._devices:
                return selector, None
        if selector.startswith("/dev/serial/by-id/") or selector.startswith("/dev/serial/by-path/"):
            return selector, None
        return None, None

    def pin_session(self, selector: str, profile_name: str) -> dict[str, Any]:
        if self._template_by_name(profile_name) is None:
            return {"ok": False, "error_code": "UNKNOWN_PROFILE"}
        device_key, session = self._resolve_device_key(selector)
        if not device_key:
            return {"ok": False, "error_code": "DEVICE_NOT_FOUND"}
        if session is not None and session.profile_source == "yaml-target":
            return {"ok": False, "error_code": "PROFILE_IS_EXPLICIT"}
        with self._lock:
            self._profile_pins[device_key] = profile_name
            self._save_state()
        return {"ok": True, "device_key": device_key, "profile": profile_name}

    def unpin_session(self, selector: str) -> dict[str, Any]:
        device_key, session = self._resolve_device_key(selector)
        if not device_key:
            return {"ok": False, "error_code": "DEVICE_NOT_FOUND"}
        if session is not None and session.profile_source == "yaml-target":
            return {"ok": False, "error_code": "PROFILE_IS_EXPLICIT"}
        with self._lock:
            self._profile_pins.pop(device_key, None)
            self._save_state()
        return {"ok": True, "device_key": device_key}

    def _attach_state_result(self, session: SessionRuntime, snap: dict[str, Any]) -> dict[str, Any]:
        """#94：依 attach 契約把 session 快照轉成回傳 dict。command-capable 卡在非 READY/ATTACHING
        的態回 ok:False + 狀態專用 error_code（FLASHING→FLASHING_BUSY、RECOVERING→SESSION_RECOVERING、
        其餘→SESSION_NOT_READY，並另外回報 last_error 欄位）；READY / ATTACHING（attach 進行中）與 passthrough（非
        command-capable）回 ok:True。統一 human-lease / bridge-present else / 同步 probe 三處邏輯，
        避免各分支 error_code 映射分歧（codex 對抗審查第三輪補洞）。"""
        state = snap.get("state")
        if session.profile.command_capable and state not in ("READY", "ATTACHING"):
            if state == "FLASHING":
                return {"ok": False, "error_code": "FLASHING_BUSY", "session": snap}
            if state == "RECOVERING":
                return {"ok": False, "error_code": "SESSION_RECOVERING", "session": snap}
            return {
                "ok": False,
                "error_code": "SESSION_NOT_READY",
                "last_error": snap.get("last_error"),
                "last_error_detail": snap.get("last_error_detail"),
                "session": snap,
            }
        return {"ok": True, "session": snap}

    def attach_session(self, selector: str) -> dict[str, Any]:
        bridge: UARTBridge | None = None
        should_probe = False
        post = _PostCloseAction()
        result: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            if not by_id:
                return {"ok": False, "error_code": "DEVICE_NOT_BOUND", "session": session.to_public_dict()}
            pending_attach_op = (
                self._active_attach_ops.get(by_id)
                or self._attach_ops.get(by_id)
                or self._preopened_ports.get(by_id)
            )
            release_in_progress = (
                pending_attach_op is not None and pending_attach_op.release_request is not None
            )
            if session._pending_drain is not None or release_in_progress:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            # C2：RELEASED 早退——比照 clear_session，不改 state、不 spawn、不動集合，
            # 避免卡死 ATTACHING 與下一次 _save_state 把 released map 寫空。
            if session.state == "RELEASED" or by_id in self._released_by_ids:
                return {
                    "ok": True,
                    "released": True,
                    "recommended_action": "device_attach",
                    "session": session.to_public_dict(),
                }
            if session.bridge is not None:
                lease, post = self._refresh_interactive_locked(session)
                if (
                    session.profile.platform == "passthrough"
                    and session.state == "ATTACHED"
                ):
                    result = {"ok": True, "session": session.to_public_dict()}
                elif lease is not None and lease.owner.startswith("human:"):
                    # #94：有 human lease 時不 probe（免干擾人類），但仍依 attach 契約誠實回報狀態
                    # （command-capable 未達 READY → ok:False + 狀態專用 error_code）。
                    result = self._attach_state_result(session, session.to_public_dict())
                elif session.state == "ATTACHED":
                    bridge = session.bridge
                    should_probe = True
                    # 手動 attach 為重新介入：允許重解析後再告警一次（#140）。
                    session.credentials_warned = False
                else:
                    # bridge 存在、無 human lease、非 ATTACHED（READY / FLASHING / RECOVERING 等）：
                    # 統一走契約，command-capable 卡在非 READY/ATTACHING → ok:False + 狀態專用 error_code。
                    result = self._attach_state_result(session, session.to_public_dict())
            if result is None and by_id not in self._devices:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                result = {"ok": False, "error_code": "DEVICE_NOT_FOUND", "session": session.to_public_dict()}
            if result is None and not should_probe:
                session.state = "ATTACHING"
                session.last_error = None
                self._reset_reprobe_progress_locked(session)
                # 手動 attach 為重新介入：允許重解析後再告警一次（#140）。
                session.credentials_warned = False
        post.execute()
        if result is not None:
            return result
        if should_probe and bridge is not None:
            # 與 auto 自動重探共用 per-session probe 互斥，避免並發 probe 同一 bridge（Finding 2）。
            with self._lock:
                probe_lock = self._reprobe_probe_lock_locked(session.session_id)
            with probe_lock:
                probe_result = self._probe_existing_bridge(session, bridge)
            # #94（codex 對抗審查補洞）：probe（lock 外阻塞）期間若 release_device 搶進使 session 變
            # RELEASED，回與早退路徑一致的 released payload（而非 helper 的 SESSION_NOT_READY/
            # STATE_CHANGED），使 RELEASED 契約在 release-during-attach race 下仍成立。
            with self._lock:
                current = self._sessions.get(session.session_id)
                cur_by_id = current.profile.device_by_id if current is not None else None
                current_attach_op = (
                    self._active_attach_ops.get(cur_by_id or "")
                    or self._attach_ops.get(cur_by_id or "")
                    or self._preopened_ports.get(cur_by_id or "")
                )
                release_in_progress = (
                    current_attach_op is not None
                    and current_attach_op.release_request is not None
                )
                if current is not None and (
                    current._pending_drain is not None or release_in_progress
                ):
                    return {
                        "ok": False,
                        "error_code": "SESSION_RECOVERING",
                        "drain_stalled": current.drain_stalled,
                        "drain_timeout_epoch": current.drain_timeout_epoch,
                        "session": current.to_public_dict(),
                    }
                if current is not None and (
                    current.state == "RELEASED"
                    or (cur_by_id and cur_by_id in self._released_by_ids)
                ):
                    return {
                        "ok": True,
                        "released": True,
                        "recommended_action": "device_attach",
                        "session": current.to_public_dict(),
                    }
            # #94：command-capable session 若同步 probe 未達 READY，attach 進入點應把埋在
            # session.last_error 的真錯浮到頂層並回 ok:False，讓 CLI/上層拿得到具體 error 與
            # 非零 exit（原本一律 ok:True、錯誤只埋巢狀 session → 上層拿到空 error）。
            # 不改共用 helper _probe_existing_bridge：reprobe/recover 仍要 stay-ATTACHED、
            # 非硬錯語意；passthrough（非 command_capable）ATTACHED 即成功，亦不改寫。
            probe_session = probe_result.get("session") if isinstance(probe_result, dict) else None
            # helper 回 ok:True 時依契約把「command-capable 未達 READY」浮成頂層 ok:False + error_code；
            # helper 已回 ok:False（STATE_CHANGED / SESSION_NOT_READY）則原樣直通。
            if probe_result.get("ok") and isinstance(probe_session, dict):
                return self._attach_state_result(session, probe_session)
            return probe_result
        self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"by_id": by_id, "real_path": dev.real_path} for by_id, dev in sorted(self._devices.items())]

    def _mark_missing_devices_locked(self) -> None:
        missing_at = now_iso()
        for session in self._sessions.values():
            by_id = session.profile.device_by_id
            if session.state == "RELEASED":
                continue
            if not by_id or session.bridge is not None:
                continue
            if by_id in self._devices:
                continue
            session.state = "DETACHED"
            if session.last_error is None:
                session.last_error = "DEVICE_NOT_FOUND"
            if session.detached_at is None:
                session.detached_at = missing_at

    def update_devices(self, devices: dict[str, DeviceInfo]) -> None:
        unconsumed_ops: list[_AttachOp] = []
        with self._lock:
            if self._shutting_down:
                import logging
                logging.getLogger("serialwrap").info("shutdown 中，略過裝置更新")
                return
            prev = self._devices
            self._devices = dict(devices)
            for by_id, op in list(self._attach_ops.items()):
                current = self._devices.get(by_id)
                if current is None or current.real_path != op.real_path:
                    op.cancelled.set()
            for by_id, op in list(self._active_attach_ops.items()):
                current = self._devices.get(by_id)
                if current is None or current.real_path != op.real_path:
                    op.cancelled.set()
                    if op.probe_bridge is not None:
                        op.probe_bridge.begin_drain(
                            op.bridge_generation,
                            preserve_consoles=True,
                            deadline=time.monotonic() + DRAIN_TIMEOUT_MS / 1000,
                        )
            for by_id, op in list(self._preopened_ports.items()):
                current = self._devices.get(by_id)
                if (
                    (current is None or current.real_path != op.real_path)
                    and by_id not in self._attach_bridges
                    and by_id not in self._active_attach_ops
                ):
                    self._preopened_ports.pop(by_id, None)
                    op.cancelled.set()
                    unconsumed_ops.append(op)
            # #100 M2/3a：device 離線後 prune 其 startup 預配（pending）COM，避免已不在線的
            # by-id 長期 reserve COM 號，導致 _next_dynamic_com 無法回收該號。
            self._pending_com = {
                b: c for b, c in self._pending_com.items() if b in self._devices
            }
            self._mark_missing_devices_locked()

        for op in unconsumed_ops:
            self._finish_attach_op(op)

        changed = sorted(
            by_id for by_id in set(prev.keys()) & set(devices.keys())
            if prev[by_id].real_path != devices[by_id].real_path
        )
        removed = sorted(set(prev.keys()) - set(devices.keys()))
        added = sorted(set(devices.keys()) - set(prev.keys()))

        changed_by_ids = set(removed) | set(changed) | set(added)
        if changed_by_ids:
            with self._lock:
                for session in self._sessions.values():
                    if session.profile.device_by_id in changed_by_ids:
                        self._reset_reprobe_progress_locked(session)

        for by_id in [*removed, *changed]:
            self._detach_by_id(by_id, reason="DEVICE_REBOUND_REQUIRED" if by_id in changed else "DEVICE_REMOVED")
        for by_id in [*added, *changed]:
            self._spawn_attach(by_id)

    def bootstrap_attach(self) -> None:
        with self._lock:
            if self._shutting_down:
                return
            keys = list(self._devices.keys())
        for by_id in keys:
            self._spawn_attach(by_id)

    def _revert_pre_marked_attaching_locked(self, by_id: str) -> None:
        """attach 因交接凍結／停止而早退時，還原鎖內預先標記的 ATTACHING 為 DETACHED（須持有 self._lock）。

        只處理尚未開啟 bridge 的 ATTACHING；否則凍結被撤銷（交接未成立）後該 session 會永久卡在 ATTACHING，
        `_prepare_reprobe_locked` 不再排它。呼叫端須確認沒有其他 attach 本體正在處理同一 by_id。
        """
        session = next((s for s in self._sessions.values() if s.profile.device_by_id == by_id), None)
        if session is not None and session.state == "ATTACHING" and session.bridge is None:
            session.state = "DETACHED"

    def _spawn_attach(self, by_id: str, *, dynamic_only: bool = False) -> None:
        """啟動 attach worker；``dynamic_only``（OD-63 佔位交接）略過 DETACHED 空槽改綁。"""
        with self._lock:
            if self._shutting_down:
                import logging
                logging.getLogger("serialwrap").info("shutdown 中，略過 attach %s", by_id)
                if by_id not in self._attach_inflight:
                    self._revert_pre_marked_attaching_locked(by_id)
                return
            if by_id in self._released_by_ids:
                return
            if by_id in self._draining_by_ids:
                return
            if by_id in self._attach_inflight:
                return
            self._attach_inflight.add(by_id)

        def _run() -> None:
            retry_after_worker = False
            try:
                if dynamic_only:
                    self._attach_by_id(by_id, dynamic_only=True)
                else:
                    self._attach_by_id(by_id)
            finally:
                with self._lock:
                    self._attach_inflight.discard(by_id)
                    self._attach_threads.pop(by_id, None)
                    session = next(
                        (row for row in self._sessions.values() if row.profile.device_by_id == by_id),
                        None,
                    )
                    retry_after_worker = bool(
                        not self._shutting_down
                        and by_id not in self._released_by_ids
                        and by_id not in self._draining_by_ids
                        and by_id in self._devices
                        and session is not None
                        and not session.released_placeholder
                        and session.bridge is None
                        and session.state == "ATTACHING"
                    )
                if retry_after_worker:
                    self._spawn_attach(by_id)

        worker = threading.Thread(target=_run, name=f"serialwrap-attach-{by_id}", daemon=True)
        with self._lock:
            self._attach_threads[by_id] = worker
        worker.start()

    def _preopen_auto_attach_port(
        self,
        session: SessionRuntime,
        by_id: str,
        real_path: str,
        profile: SessionProfile,
    ) -> _AttachOp | None:
        """自動 attach 先取得 UART；成功前不把 session 公開成 ATTACHING。"""
        with self._lock:
            if (
                self._shutting_down
                or session.bridge is not None
                or session.state == "RELEASED"
                or by_id in self._released_by_ids
            ):
                return None
            existing = self._preopened_ports.get(by_id)
            if existing is not None:
                return existing
            if (
                by_id in self._attach_ops
                or by_id in self._active_attach_ops
                or by_id in self._draining_by_ids
            ):
                return None
            if self._sessions.get(session.session_id) is not session:
                return None
            if session.profile.device_by_id != by_id:
                return None
            device = self._devices.get(by_id)
            if device is None or device.real_path != real_path:
                return None
            op = _AttachOp(
                token=object(),
                session_id=session.session_id,
                by_id=by_id,
                bridge_generation=session.bridge_generation,
                real_path=real_path,
            )
            self._attach_ops[by_id] = op

        port: SerialPort | None = None
        try:
            port = uart_io_mod.open_serial_port(real_path, profile.uart)
            with op.port_lock:
                op.port = port
            port.open()
        except Exception as exc:
            with self._lock:
                cancelled = op.cancelled.is_set() or op.release_request is not None
                if self._attach_ops.get(by_id) is op:
                    self._attach_ops.pop(by_id, None)
                    current = self._sessions.get(session.session_id)
                    if (
                        not cancelled
                        and current is session
                        and session.profile.device_by_id == by_id
                        and session.bridge_generation == op.bridge_generation
                        and session.state != "RELEASED"
                        and by_id not in self._released_by_ids
                    ):
                        session.state = "DETACHED"
                        session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                        session.detached_at = now_iso()
            self._finish_attach_op(op)
            return None

        assert port is not None
        with self._lock:
            device = self._devices.get(by_id)
            valid = bool(
                self._attach_ops.get(by_id) is op
                and not op.cancelled.is_set()
                and self._sessions.get(session.session_id) is session
                and session.profile.device_by_id == by_id
                and session.bridge_generation == op.bridge_generation
                and session.state != "RELEASED"
                and by_id not in self._released_by_ids
                and session._pending_drain is None
                and by_id not in self._draining_by_ids
                and not self._shutting_down
                and device is not None
                and device.real_path == real_path
            )
            if valid:
                op.port = port
                self._active_attach_ops[by_id] = op
                self._attach_ops.pop(by_id, None)
                session.state = "ATTACHING"
                session.last_error = None
                self._reset_reprobe_progress_locked(session)
                return op
            if self._attach_ops.get(by_id) is op:
                self._attach_ops.pop(by_id, None)

        self._finish_attach_op(op)
        return None

    def _preopen_dynamic_attach_port(
        self,
        by_id: str,
        real_path: str,
        profile: UartProfile,
    ) -> tuple[_AttachOp | None, Exception | None]:
        """動態偵測前先獨占開埠，讓 PROBE 與正式 bridge 共用同一 handle。"""
        with self._lock:
            if self._shutting_down or by_id in self._draining_by_ids:
                return None, None
            existing = self._preopened_ports.get(by_id)
            if existing is not None:
                return existing, None
            if by_id in self._attach_ops or by_id in self._active_attach_ops:
                return None, None
            device = self._devices.get(by_id)
            if device is None or device.real_path != real_path:
                return None, None
            if any(row.profile.device_by_id == by_id for row in self._sessions.values()):
                return None, None
            op = _AttachOp(
                token=object(),
                session_id=f"dynamic:{by_id}",
                by_id=by_id,
                bridge_generation=0,
                real_path=real_path,
            )
            self._attach_ops[by_id] = op

        port: SerialPort | None = None
        try:
            port = uart_io_mod.open_serial_port(real_path, profile)
            with op.port_lock:
                op.port = port
            port.open()
        except Exception as exc:
            with self._lock:
                if self._attach_ops.get(by_id) is op:
                    self._attach_ops.pop(by_id, None)
            self._finish_attach_op(op)
            return None, exc

        assert port is not None
        with self._lock:
            device = self._devices.get(by_id)
            valid = bool(
                self._attach_ops.get(by_id) is op
                and not op.cancelled.is_set()
                and not self._shutting_down
                and by_id not in self._draining_by_ids
                and device is not None
                and device.real_path == real_path
                and not any(row.profile.device_by_id == by_id for row in self._sessions.values())
            )
            if valid:
                op.port = port
                self._active_attach_ops[by_id] = op
                self._attach_ops.pop(by_id, None)
                return op, None
            if self._attach_ops.get(by_id) is op:
                self._attach_ops.pop(by_id, None)
        self._finish_attach_op(op)
        return None, None

    def _discard_preopened_port(self, by_id: str, op: _AttachOp | None) -> None:
        """移除並關閉仍由 manager 保管、尚未交給正式 bridge 的 handle。"""
        if op is None:
            return
        self._finish_attach_op(op)

    def _finish_attach_op(self, op: _AttachOp) -> bool:
        """在 bridge 已安全交還或未啟動時，關閉並完成 attach op。"""
        with op.port_lock:
            if op.port is not None and not op.port_transferred:
                try:
                    op.port.close()
                except Exception as exc:  # noqa: BLE001 — handle 狀態不明時維持阻擋
                    import logging

                    logging.getLogger("serialwrap").warning(
                        "預開 UART handle 關閉失敗（by_id=%s）：%s", op.by_id, exc
                    )
                    return False
                op.port = None

        with self._lock:
            for table in (self._attach_ops, self._preopened_ports, self._active_attach_ops):
                if table.get(op.by_id) is op:
                    table.pop(op.by_id, None)
            release_request = op.release_request
            session = self._sessions.get(op.session_id)
            if release_request is not None and session is not None:
                source, reason = release_request
                if session.state != "RELEASED":
                    self._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
                session.state = "RELEASED"
                session.released_by = source
                session.released_at = session.released_at or now_iso()
                session.released_reason = reason
                session.drain_stalled = False
                session.drain_timeout_epoch = None
                session._release_persist_pending = True
                self._released_by_ids.add(op.by_id)
        if release_request is not None and session is not None:
            try:
                self._persist_release_state(session.session_id)
            except Exception as exc:  # noqa: BLE001 — 失敗時保持 RELEASED 阻擋並留下診斷
                import logging

                logging.getLogger("serialwrap").error(
                    "release 持久化失敗（session=%s）：%s", session.session_id, exc
                )
                op.done.set()
                return False
        op.done.set()
        return True

    def _register_probe_bridge(self, op: _AttachOp, bridge: "UARTBridge") -> bool:
        with self._lock:
            if op.cancelled.is_set() or self._active_attach_ops.get(op.by_id) is not op:
                return False
            op.probe_bridge = bridge
            return True

    def _handoff_attach_op_locked(self, op: _AttachOp | None) -> bool:
        """在同一把 manager lock 內把 handle 所有權轉給正式 bridge。"""
        if op is None or op.cancelled.is_set():
            return op is None
        with op.port_lock:
            op.port_transferred = True
            op.port = None
        if self._active_attach_ops.get(op.by_id) is op:
            self._active_attach_ops.pop(op.by_id, None)
        op.done.set()
        return True

    def _stop_probe_bridge_safely(self, bridge: "UARTBridge") -> bool:
        """等待 PROBE 的 reader／writer 收斂；逾時時等候同一 drain 的遲到回條。"""
        done = threading.Event()
        deadline = time.monotonic() + DRAIN_TIMEOUT_MS / 1000
        bridge.begin_drain(-1, preserve_consoles=False, deadline=deadline)
        receipt = bridge.finish_drain(
            max(0.0, deadline - time.monotonic()),
            on_late_receipt=lambda _receipt: done.set(),
        )
        if receipt is None:
            done.wait()
            with bridge._state_lock:
                receipt = bridge._drain_receipt
        return self._receipt_is_safe(receipt)

    def _record_dynamic_open_failure(
        self,
        by_id: str,
        real_path: str,
        error: Exception,
        *,
        template: ProfileTemplate | None,
        source: str | None,
    ) -> None:
        """動態 attach 開埠失敗時保留可診斷的 DETACHED session。"""
        notify_session_id: str | None = None
        with self._lock:
            device = self._devices.get(by_id)
            if (
                self._shutting_down
                or by_id in self._draining_by_ids
                or device is None
                or device.real_path != real_path
                or by_id in self._released_by_ids
            ):
                return
            session = next(
                (row for row in self._sessions.values() if row.profile.device_by_id == by_id),
                None,
            )
            if session is not None and session.state == "RELEASED":
                return
            if session is None:
                if len(self._sessions) >= self._max_sessions:
                    return
                selected_template = template or self._default_passthrough_template()
                if selected_template is None:
                    return
                session = self._session_from_template(selected_template, by_id)
                session.profile_source = source if template is not None and source else "fallback"
            session.state = "DETACHED"
            session.last_error = f"ATTACH_FAILED:{type(error).__name__}"
            session.detached_at = now_iso()
            notify_session_id = session.session_id
        if notify_session_id is not None:
            self._on_detached(notify_session_id)

    def _detach_by_id(self, by_id: str, *, reason: str) -> None:
        started_at = time.monotonic()
        jobs: list[_DrainJob] = []
        with self._lock:
            # flash 臨界區（FLASHING 或 probe 進行中）不得 detach（#83 RACE-2 final）：device hotplug 在
            # MCU reset/BSL 期間常見地讓 by_id 短暫消失，若據此 detach 會切斷燒錄/偵測 transport。略過；
            # flash 結束（exit_flashing/unmark）後下一個 device_watcher tick 會再 reconcile。
            targets = [
                s for s in self._sessions.values()
                if s.profile.device_by_id == by_id and s.state != "RELEASED"
                and not self._flash_busy_locked(s)
            ]
            for session in targets:
                if session._pending_drain is not None:
                    continue
                kind = "device_removed" if reason == "DEVICE_REMOVED" else "recover"
                job = self._begin_drain_locked(
                    session,
                    kind=kind,
                    reason=reason,
                    drop_consoles=False,
                    started_at=started_at,
                )
                if job is not None:
                    jobs.append(job)
                else:
                    session.state = "DETACHED"
                    self._detach_session_locked(session, reason=reason, drop_consoles=False)
        for job in jobs:
            self._on_detached(job.session_id)
            self._await_drain(job)

    def _mark_session_rx(self, session: SessionRuntime) -> None:
        """更新 RX 時刻；passthrough ATTACHED 的每筆 RX 也重設安靜計時。"""
        session.last_rx_at = now_iso()
        session.last_rx_mono = time.monotonic()
        if session.profile.platform == "passthrough" and session.state == "ATTACHED":
            session.passthrough_quiet_since = session.last_rx_mono
        # RX 恢復即重臂 transport stall 一次性告警（#150）：下次再 stall 允許再告警一次。
        session.transport_stall_warned = False

    def passthrough_ready_in_s(self, session: SessionRuntime, now: float) -> float | None:
        """回報 passthrough ATTACHED session 進入 READY 尚需的安靜秒數。"""
        if session.profile.platform != "passthrough" or session.state != "ATTACHED":
            return None
        quiet_since = session.passthrough_quiet_since
        if quiet_since is None:
            return round(PASSTHROUGH_READY_QUIET_S, 1)
        return round(max(0.0, PASSTHROUGH_READY_QUIET_S - (now - quiet_since)), 1)

    def _promote_quiet_passthrough_locked(self, now: float) -> list[str]:
        """將通過 30 秒 RX 靜默且沒有阻擋條件的 passthrough session 升為 READY。"""
        if self._shutting_down:
            return []
        promoted: list[str] = []
        for session in self._sessions.values():
            if session.profile.platform != "passthrough" or session.state != "ATTACHED":
                continue
            by_id = session.profile.device_by_id
            if (
                session.bridge is None
                or session._pending_drain is not None
                or (by_id is not None and by_id in self._draining_by_ids)
                or (by_id is not None and by_id in self._released_by_ids)
                or self._flash_busy_locked(session)
            ):
                continue
            pending_op = (
                self._active_attach_ops.get(by_id or "")
                or self._attach_ops.get(by_id or "")
                or self._preopened_ports.get(by_id or "")
            )
            if pending_op is not None and getattr(pending_op, "release_request", None) is not None:
                continue
            quiet_since = session.passthrough_quiet_since
            if quiet_since is None:
                session.passthrough_quiet_since = now
                continue
            if now - quiet_since < PASSTHROUGH_READY_QUIET_S:
                continue
            session.state = "READY"
            session.confirm_ready()
            session.last_error = None
            session.last_ready_at = now_iso()
            session.recovering = False
            session.recovery_started_at = None
            session.pending_auto_login = False
            self._reset_reprobe_progress_locked(session)
            promoted.append(session.session_id)
        return promoted

    def _update_boot_quiet_locked(self, session: SessionRuntime, chunk: str) -> None:
        """依 RX 內容維護 boot quiet window（#130）。須在 ``self._lock`` 內呼叫。

        - RX 見到 boot banner（U-Boot 版本行／autoboot 倒數）→ 啟用／延長 quiet
          window。``arm_boot_quiet`` 同時清空 rolling tail，使 banner 之前殘留的
          舊 prompt 不會誤觸發解除（DUT 自行重開機、無人先呼叫 reboot 的情境）。
        - quiet window 進行中，RX 匹配該 session template 的 ``login_regex``、
          ``prompt_regex`` 或 ``boot_done_regex``（開機完成訊號）→ 立即解除，讓
          recovery / reprobe 恢復探測。
          但尾行先命中 ``bootloader_prompts``（見下方 Finding 3 說明）時例外不解除。
        - gate source=system 的自動 probe 與（#139）非 human 來源的 agent 顯式命令
          （submit/execute/file 傳輸雙層 gate）；不 gate human console／lease TX。

        比對用 rolling tail（``BOOT_BANNER_TAIL_CHARS`` 字元）跨 chunk 邊界拼接，
        容忍 banner／prompt 被 RX 讀取切割。

        #162 回歸修：**比對窗＝舊 tail 尾段 + 整個 chunk（不截尾）**，只有「保存」
        才截到 ``BOOT_BANNER_TAIL_CHARS``。舊碼 ``(tail + chunk)[-256:]`` 對單一
        chunk 長度 > 256 的情況會把 chunk **開頭** 截掉——實機 WAL seq=79（345 字元，
        含 ``U-Boot TPL 2024.04``）與 seq=88（354 字元，含 ``Found FIT format U-Boot``）
        兩個 banner chunk 都因此漏判，quiet 晚了 0.85s 才 arm，正好讓 CTRL-C/CTRL-D
        兩發都通過 gate。rolling tail 原本的用途（跨 chunk 切割的 banner）不受影響：
        舊 tail 仍完整接在 chunk 前面。
        """
        if session.profile.platform == "passthrough":
            return
        window = session.boot_banner_tail[-BOOT_BANNER_TAIL_CHARS:] + chunk
        if detect_boot_banner(window):
            session.arm_boot_quiet()
            return
        tail = window[-BOOT_BANNER_TAIL_CHARS:]
        session.boot_banner_tail = tail
        if session.boot_quiet_until <= 0.0:
            return
        sp = session.profile
        # #130 review Finding 3（必修）：tail 尾行若先命中該 template 的
        # bootloader_prompts，視為仍在 bootloader、非開機完成，本輪不解除。寬鬆
        # template（如 brcm-template `[>#]\s*$`）的 prompt_regex 會誤配 U-Boot
        # 自己的 `=> ` prompt，若不先排除，會在板子正卡 bootloader 的最危險時刻
        # 解除 quiet window，讓後續自動 probe 又打斷一次 autoboot。
        if _matches_any_bootloader_prompt(tail, sp.bootloader_prompts) is not None:
            return
        # boot_done_regex：profile 宣告的開機完成訊號（例：prpl/OpenWrt askconsole 的
        # 「Please press Enter to activate this console」）。askconsole 不匹配 login／prompt，
        # 未設時只能等 BOOT_QUIET_WINDOW_S 過期；文案放 profile、不寫死在程式碼。
        for pattern in (sp.login_regex, sp.prompt_regex, sp.boot_done_regex):
            if not pattern:
                continue
            try:
                if re.search(pattern, tail):
                    session.clear_boot_quiet()
                    return
            except re.error:
                continue

    def _mark_session_tx(self, session: SessionRuntime) -> None:
        """Update session's last_tx_at / last_tx_mono. Cheap; safe outside lock."""
        session.last_tx_at = now_iso()
        session.last_tx_mono = time.monotonic()

    def revoke_tx_op(self, cmd_id: str) -> None:
        """取消 arbiter 命令時撤銷所有持有中的 bridge permit。"""
        targets: list[tuple[UARTBridge, list[str]]] = []
        with self._lock:
            for session in self._sessions.values():
                operation = session._foreground_operation
                if operation is None or operation.cmd_id != cmd_id:
                    continue
                op_ids = [cmd_id]
                if session.interactive_session_id is not None:
                    op_ids.append(f"interactive:{session.interactive_session_id}")
                targets.append((operation.bridge, op_ids))
            if not targets:
                self._cancelled_tx_cmd_ids[cmd_id] = None
                self._cancelled_tx_cmd_ids.move_to_end(cmd_id)
                while len(self._cancelled_tx_cmd_ids) > 512:
                    self._cancelled_tx_cmd_ids.pop(next(iter(self._cancelled_tx_cmd_ids)))
        # fence lock 不可在 manager lock 內等待，尤其 Windows pyserial write 可能阻塞。
        for bridge, op_ids in targets:
            revoke = getattr(bridge, "revoke_ops", None)
            if callable(revoke):
                revoke(op_ids)

    def _on_bridge_console_line(self, session_id: str, client_id: str, line: str) -> None:
        if self._on_console_line is not None:
            self._on_console_line(session_id, client_id, line)

    def _on_bridge_rx(
        self,
        session_id: str,
        data: bytes,
        wal_seq: int | None = None,
    ) -> None:
        # Notify RX observers before foreground_busy gate (observers receive ALL rx).
        # IMPORTANT: Observer MUST NOT block on I/O or cross-thread wait, and MUST NOT
        # call SessionManager methods under _lock (deadlock risk). Exceptions are silently swallowed.
        with self._lock:
            session = self._sessions.get(session_id)
            com = session.profile.com if session is not None else session_id
            chunk_wal_seq = wal_seq if wal_seq is not None else self._wal.current_seq
            observers = list(self._rx_observers)
        if observers:
            for obs in observers:
                try:
                    obs(com, data, chunk_wal_seq)
                except Exception:
                    pass
        chunk = clean_text(data.decode("utf-8", errors="replace"))
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if chunk or session.profile.platform == "passthrough":
                self._mark_session_rx(session)
            if not chunk:
                return
            # #130：quiet window 維護須看見**所有** RX（含 foreground 期間），
            # 故置於 foreground_busy gate 之前。
            self._update_boot_quiet_locked(session, chunk)
            # background capture 無條件接收 RX（#159）：不受 foreground_busy 影響——
            # background 命令自己送出後、等 prompt 比對成功前的整段等待期間
            # foreground_busy 也是 True（fg/bg 共用同一段等待邏輯），若被 gate 掉，
            # 快速完成的命令會在 chunk 機制真正掛上前就把全部輸出送完。
            # 已終結（status != "active"）的 capture 仍會被 continue 跳過；新的
            # 非-background 命令開始時，既有 active background capture 已在
            # _execute_command_inner 進入 busy 前先行終結，故不會被新命令自己的
            # TX echo 汙染。
            for cmd_id in list(session.background_cmd_ids):
                capture = self._background.get(cmd_id)
                if capture is None or capture.status != "active":
                    continue
                capture.add_chunk(chunk, BG_CAPTURE_MAX_BYTES)
                capture.last_activity_mono = time.monotonic()
                capture.last_seq = chunk_wal_seq
            if session.foreground_busy:
                return
            # agent log capture（維持原行為：foreground 期間不寫 agent log）
            cap = session.active_capture
            if cap is not None and cap.status == "active":
                fp = self._capture_fps.get(cap.capture_id)
                if fp is not None:
                    try:
                        fp.write(chunk)
                        fp.flush()
                        cap.byte_count += len(chunk.encode("utf-8"))
                        cap.line_count += chunk.count("\n")
                    except Exception:
                        pass

    def _handle_bridge_down(self, session_id: str, bridge: UARTBridge, reason: str) -> None:
        started_at = time.monotonic()
        by_id: str | None = None
        job: _DrainJob | None = None
        with self._lock:
            # callback 使用 canonical session_id；legacy 呼叫端可傳 COM 或 alias。
            session = self.get_session(session_id)
            if session is None or session.bridge is not bridge:
                return
            # flash 臨界區（FLASHING 或 probe 進行中）不得自動 detach+reattach（#83 RACE-2 final）：燒錄/
            # 偵測期間 bridge 由 daemon 持有為唯一 reader，不應因 bridge-down 回呼搶進 ATTACHING、另起新
            # attach 與 flash pump 對撞。略過；flash 結束後若 bridge 真的壞了，pump 收尾與後續 reconcile 處理。
            if self._flash_busy_locked(session):
                return
            by_id = session.profile.device_by_id
            job = self._begin_drain_locked(
                session,
                kind="io_error",
                reason=f"BRIDGE_DOWN:{reason}",
                drop_consoles=False,
                started_at=started_at,
            )
        if job is not None:
            self._on_detached(session.session_id)
            self._await_drain(job)

    @staticmethod
    def _handoff_frozen_probe_result_locked(session: SessionRuntime) -> dict[str, Any]:
        """交接凍結後 probe 的拒絕結果：零 TX、零狀態改寫（須持有 self._lock，且不得修改 session）。"""
        return {
            "ok": False,
            "error_code": "FLUSHED_BY_SHUTDOWN",
            "reason": "handoff_frozen",
            "session": session.to_public_dict(),
        }

    def _probe_existing_bridge(self, session: SessionRuntime, bridge: UARTBridge) -> dict[str, Any]:
        if session.profile.platform == "passthrough":
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            return {"ok": True, "session": current.to_public_dict()}
        # 非 command-capable（無 ready_probe，含 passthrough）維持現狀不升 READY。
        if not session.profile.command_capable:
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            return {"ok": True, "session": current.to_public_dict()}

        # #130 review Finding 2（必修）：本 helper 是 attach_session（ATTACHED 無 human
        # lease 分支）與 recover_session（ATTACHED 分支）共用的唯一 probe 入口——boot
        # quiet window 內於此單點 gate，可同時堵住兩條呼叫路徑，避免各呼叫端各自補一次
        # 判斷、日後新增第三個呼叫端又漏補（原始漏洞正是「多個呼叫端各自漏 gate」）。
        # 比照 _attach_by_id 的 PROMPT_UNAVAILABLE 語意：不送 probe TX、不改動
        # session.state（呼叫端進來時已是 ATTACHED），只誠實回報仍在 quiet window。
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            if self._handoff_frozen:
                # SC-11：交接已凍結，不得再開始 probe（登入世代與 probe 授權亦不得在凍結後產生）。
                return self._handoff_frozen_probe_result_locked(current)
            if self._boot_quiet_blocks_probe(current):
                # #162：READY（再確認 pending）走到這裡＝quiet 又被 re-arm——不得以
                # 合成 PROMPT_UNAVAILABLE 覆寫 READY 的 last_error（名義 READY 的
                # last_error 恆 None，覆寫會讓 session list 呈現矛盾）；ATTACHED
                # 維持 #130 原行為（誠實回報仍在 quiet window）。
                if current.state != "READY":
                    current.last_error = "PROMPT_UNAVAILABLE"
                return {"ok": True, "session": current.to_public_dict()}
            probe_permit = self._begin_login_run_locked(current, bridge)

        # #150：probe 前採樣 raw RX 累計，失敗後取差精煉 transport stall。
        rx_before = self._bridge_rx_total(bridge)

        probe_refused = False
        try:
            with self._system_permit_context(bridge, probe_permit):
                if session.profile.login_regex or session.profile.console_unlock_env:
                    auth, auth_res = resolve_session_auth(session.profile)
                    if self._credentials_declared_but_unresolved(session.profile, auth_res):
                        # 宣告帳密但解析空：不進 login/probe（不對 prompt 送空帳密），標終態（#140）。
                        self._emit_credentials_unresolved_warning(
                            session.session_id, session.profile.com, auth_res
                        )
                        ok, err = False, ERROR_CREDENTIALS_UNRESOLVED
                    elif auth.console_unlock or (auth.username and auth.password):
                        ok, err = ensure_ready(bridge, session.profile, auth=auth)
                    else:
                        ok, err = probe_ready(bridge, session.profile)
                else:
                    ok, err = probe_ready(bridge, session.profile)
        except TxIncompleteError as exc:
            if not _login_tx_refused(exc):
                raise
            # submit fence 已證明此 run 被 quiet 或更新登入世代撤銷；不將零 TX
            # 偽裝成 transport stall，也不讓 READY reconfirm 被提前清除。
            ok, err = False, "PROMPT_UNAVAILABLE"
            probe_refused = True

        # #150：真的送過 probe 且失敗 → transport 層精煉（CREDENTIALS_UNRESOLVED
        # 不屬可精煉集合，經此為 no-op）。
        err_detail: str | None = None
        if not ok and not probe_refused:
            err, err_detail = self._refine_probe_failure(session, bridge, err, rx_before)

        notify_ready = False
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            # SC-11：probe 於鎖外執行期間可能已完成交接凍結。凍結檢查位於同一把 manager lock 內、
            # 任何狀態修改（含下方 READY 寫入點與 ATTACHED 錯誤寫入）之前；凍結後 probe 結果不得改寫 session。
            if self._handoff_frozen:
                return self._handoff_frozen_probe_result_locked(current)
            # probe 在 lock 外阻塞期間，session 可能被 flash broker 搶進 FLASHING、或被 device
            # handoff 釋放成 RELEASED。此時不得用 probe 結果覆寫狀態，否則會把 FLASHING 打回
            # ATTACHED → flasher 結束時 exit_flashing 見 state≠FLASHING 提早 return、bridge 永久
            # 卡在 flash 模式並靜默丟棄非 flash 寫入（#69 Finding 1 round2）。
            if current.state in {"FLASHING", "RELEASED"}:
                return {"ok": False, "error_code": "STATE_CHANGED", "session": current.to_public_dict()}
            current.recovering = False
            current.recovery_started_at = None
            current.pending_auto_login = False
            if ok:
                current.state = "READY"
                # #139：READY＝ready probe 實際成功＝板已開完機；殘留 quiet window
                # （probe 期間 banner 才 arm、尚未被 prompt 解除／過期）不得再 gate
                # agent 顯式命令——保證「READY 後立即 submit 必成功」恆真，gate 生效
                # 窗口精確等於「banner arm 之後、session 尚未重新確認 READY 之前」。
                # 已知殘留競態（#139 risks，需知悉）：probe 在 lock 外成功後、本 lock
                # 內 READY 落定前，真 banner 恰好 arm 會被此 clear 抹掉一次——依賴
                # U-Boot 多行 banner／倒數行後續 RX re-arm 補位，殘留窗小但非零。
                # #162：READY 確認點——同步清 ready_reconfirm_pending（agent gate 解除）。
                current.confirm_ready()
                current.last_error = None
                current.last_ready_at = now_iso()
                self._reset_reprobe_progress_locked(current)
                notify_ready = True
            elif probe_refused and current.state == "READY":
                # READY reconfirm 在 pre-submit 被 quiet／新登入世代撤銷時，保留
                # READY 與既有 last_error；`confirm_ready()` 仍未執行，pending 不清除。
                current.last_error_detail = None
            else:
                current.state = "ATTACHED"
                current.last_error = err
                current.last_error_detail = err_detail
            result = current.to_public_dict()
        if notify_ready:
            self._on_ready(session.session_id)
        return {"ok": True, "session": result}

    def _attach_by_id(self, by_id: str, *, dynamic_only: bool = False) -> None:
        save_needed = False
        dynamic_created = False
        capacity_rejected = False
        preopened_op: _AttachOp | None = None
        with self._lock:
            if self._shutting_down:
                import logging
                logging.getLogger("serialwrap").info("shutdown 中，略過 attach %s", by_id)
                self._revert_pre_marked_attaching_locked(by_id)
                return
            if by_id in self._draining_by_ids:
                return
            session = next((s for s in self._sessions.values() if s.profile.device_by_id == by_id), None)
            if session is not None and session.bridge is not None:
                return
            preopened_op = self._preopened_ports.pop(by_id, None)
            if preopened_op is not None:
                self._active_attach_ops[by_id] = preopened_op
            # OD-63 防線：佔位 session 一律不建 bridge（只能經 device attach 交 template 偵測）。
            placeholder_blocked = session is not None and session.released_placeholder
            if session is None and not dynamic_only:
                candidates = sorted(
                    [s for s in self._sessions.values() if s.state == "DETACHED" and s.profile.device_by_id not in self._devices],
                    key=lambda row: row.profile.act_no,
                )
                if candidates:
                    session = candidates[0]
                    session.profile = dataclasses.replace(session.profile, device_by_id=by_id)
                    self._binding_overrides[session.session_id] = by_id
                    save_needed = True
            # 若仍無匹配且有 templates → 嘗試動態偵測
            if session is None and self._templates:
                if len(self._sessions) >= self._max_sessions:
                    import logging
                    logging.getLogger("serialwrap").warning(
                        "已達 max_sessions=%d，忽略裝置 %s", self._max_sessions, by_id,
                    )
                    capacity_rejected = True
                else:
                    dynamic_created = True
        if save_needed:
            self._save_state()

        if placeholder_blocked:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return

        # --- 動態偵測路徑 ---
        if dynamic_created:
            self._attach_by_id_dynamic(by_id, preopened_op=preopened_op)
            return

        if capacity_rejected:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return

        if session is None:
            if dynamic_only:
                with self._lock:
                    self._pending_com.pop(by_id, None)
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return
        missing_device = False
        with self._lock:
            device = self._devices.get(by_id)
            if device is None:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                missing_device = True
            else:
                real_path = device.real_path
                profile = session.profile
                preopened_op = self._preopened_ports.get(by_id) or preopened_op
        if missing_device:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return
        if preopened_op is None:
            # 預先開埠成功後才公開 ATTACHING；已識別 holder 時保留 DETACHED，且 bridge 不會 TX。
            preopened_op = self._preopen_auto_attach_port(
                session, by_id, real_path, profile
            )
            if preopened_op is None:
                return

        # 既有 session 的 profile 再解析（#181）：pin 逃生口與「fallback 只是暫時分類」。
        # 必須在下方開 bridge 之前做——偵測需要自己的 PROBE bridge，且換 template 會連
        # uart 參數（baud 等）一起換掉。本方法原地 mutate 同一個 session 物件，故上面
        # 取得的 session 區域變數在此之後仍然有效。
        if not self._reresolve_profile_on_reattach(by_id, preopened_op=preopened_op):
            return

        abort_attach = False
        with self._lock:
            dev = self._devices.get(by_id)
            if (
                self._shutting_down
                or by_id in self._draining_by_ids
                or session.bridge is not None
                or session.profile.device_by_id != by_id
                or session.state == "RELEASED"
                or by_id in self._released_by_ids
                or (preopened_op is not None and preopened_op.cancelled.is_set())
            ):
                abort_attach = True
            elif dev is None:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                abort_attach = True
            if not abort_attach:
                assert dev is not None
                session.state = "ATTACHING"
                session.last_error = None
                gen_before = session.bridge_generation
                session_id = session.session_id
                profile = session.profile
                passthrough = profile.platform == "passthrough"
                require_login = not passthrough and (
                    session.pending_auto_login or bool(profile.login_regex or profile.console_unlock_env)
                )
                # passthrough 一律停在 ATTACHED；ready_probe 只描述命令能力，不授權 attach probe。
                command_capable = profile.command_capable and not passthrough
                real_path = dev.real_path
                preserved_consoles = session.retained_consoles if isinstance(session.retained_consoles, PreservedConsoles) else None
        if abort_attach:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return

        bridge = UARTBridge(
            profile.com,
            real_path,
            profile.uart,
            self._wal,
            on_console_line=lambda client_id, line, sid=session_id: self._on_bridge_console_line(sid, client_id, line),
            on_rx_data=lambda data, wal_seq=None, sid=session_id: self._on_bridge_rx(
                sid, data, wal_seq
            ),
            on_bridge_down=lambda reason, sid=session_id: self._handle_bridge_down(sid, bridge, reason),
            preserved_consoles=preserved_consoles,
            preopened_port=preopened_op.port if preopened_op is not None else None,
            owns_port=True,
        )

        attach_started = threading.Event()
        abort_attach = False
        with self._lock:
            if (
                self._shutting_down
                or by_id in self._draining_by_ids
                or session.state == "RELEASED"
                or by_id in self._released_by_ids
                or (preopened_op is not None and preopened_op.cancelled.is_set())
            ):
                abort_attach = True
            elif self._handoff_attach_op_locked(preopened_op):
                self._attach_bridges[by_id] = bridge
                self._attach_start_events[by_id] = attach_started
                session.attach_bridge = bridge
            else:
                abort_attach = True
        if abort_attach:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return

        try:
            try:
                bridge.start()
            finally:
                attach_started.set()
            with self._lock:
                pending_job = self._draining_by_ids.get(by_id)
                if pending_job is None and self._shutting_down:
                    pending_job = self._begin_attach_bridge_stop_locked(
                        session,
                        by_id,
                        bridge,
                        preserve_consoles=True,
                        started_at=time.monotonic(),
                    )
            if pending_job is not None:
                if pending_job.kind == "attach_cleanup":
                    self._finish_attach_bridge_stop(pending_job)
                return
            # #150：probe 前採樣 raw RX 累計（bridge 剛 start，計數從 0 起）。
            rx_before = self._bridge_rx_total(bridge)
            with self._lock:
                boot_quiet = (
                    profile.platform != "passthrough"
                    and self._boot_quiet_blocks_probe(session)
                )
                probe_permit = (
                    self._begin_login_run_locked(session, bridge)
                    if command_capable and not boot_quiet
                    else None
                )
            probe_refused = False
            used_ensure_ready = False
            if not command_capable:
                ok = False
                err = None
            elif boot_quiet:
                # #130：boot quiet window 內不送 attach probe（涵蓋 DUT reboot 期間
                # 序列裝置 re-enumerate 觸發的 re-attach）：掛上 bridge 純收 RX，
                # 以 PROMPT_UNAVAILABLE 停在 ATTACHED——window 解除後由既有
                # reprobe / reboot recovery 流程接手升 READY。
                ok, err = False, "PROMPT_UNAVAILABLE"
            else:
                try:
                    with self._system_permit_context(bridge, probe_permit):
                        if require_login:
                            auth, auth_res = resolve_session_auth(profile)
                            if self._credentials_declared_but_unresolved(profile, auth_res):
                                # 宣告帳密但解析空：不 probe/login，掛橋停 ATTACHED 終態（#140）。
                                self._emit_credentials_unresolved_warning(
                                    session_id, profile.com, auth_res
                                )
                                ok, err = False, ERROR_CREDENTIALS_UNRESOLVED
                            elif auth.console_unlock or (auth.username and auth.password):
                                used_ensure_ready = True
                                ok, err = ensure_ready(bridge, profile, auth=auth)
                            else:
                                # 無帳密時退回 probe，讓 human 手動登入
                                ok, err = probe_ready(bridge, profile)
                        else:
                            ok, err = probe_ready(bridge, profile)
                except TxIncompleteError as exc:
                    if not _login_tx_refused(exc):
                        raise
                    ok, err = False, "PROMPT_UNAVAILABLE"
                    probe_refused = True
                if used_ensure_ready and not ok and not probe_refused:
                    # #150：於 bridge.stop() 前精煉（stop 後計數仍可讀，但語意上先取）。
                    err, err_detail = self._refine_probe_failure(session, bridge, err, rx_before)
                    receipt = self._track_attach_bridge_stop(session, by_id, bridge)
                    preserved = getattr(receipt, "preserved", None)
                    with self._lock:
                        self._store_retained_consoles_locked(
                            session,
                            preserved,
                            human_owner=session.retained_human_owner,
                            human_timeout_s=session.retained_human_timeout_s,
                        )
                        session.state = "DETACHED"
                        session.last_error = err
                        session.last_error_detail = err_detail
                        session.detached_at = now_iso()
                        session.bridge = None
                        session.attached_real_path = None
                    self._on_detached(session.session_id)
                    return

            # #150：失敗才精煉；boot-quiet 合成的 PROMPT_UNAVAILABLE 沒送 probe、
            # 且 quiet 代表剛有 RX，刻意排除不精煉。
            err_detail: str | None = None
            if (
                not ok
                and not boot_quiet
                and not probe_refused
                and profile.platform != "passthrough"
            ):
                err, err_detail = self._refine_probe_failure(session, bridge, err, rx_before)

            notify_ready = False
            cleanup_job = None
            cleanup_reason = None
            with self._lock:
                # C1 backstop：release 落在 attach 飛行窗口（bridge.start()+probe 耗時）內時，
                # 關掉剛開的 FD（clean slate），但**保留 RELEASED**——不可打回 DETACHED。
                if self._shutting_down or session.state == "RELEASED" or by_id in self._released_by_ids:
                    cleanup_reason = "released"
                    pending_job = self._draining_by_ids.get(by_id)
                    cleanup_job = pending_job if pending_job is not None and pending_job.bridge is bridge else None
                    if cleanup_job is None and self._attach_bridges.get(by_id) is bridge:
                        cleanup_job = self._begin_attach_bridge_stop_locked(
                            session,
                            by_id,
                            bridge,
                            preserve_consoles=False,
                            started_at=time.monotonic(),
                        )
                else:
                    current = self._devices.get(by_id)
                    if current is None or current.real_path != real_path or session.state == "DETACHED" or session.bridge_generation != gen_before:
                        cleanup_reason = "removed"
                        cleanup_job = self._begin_attach_bridge_stop_locked(
                            session,
                            by_id,
                            bridge,
                            preserve_consoles=True,
                            started_at=time.monotonic(),
                        )
                        session.state = "DETACHED"
                        session.last_error = "DEVICE_REMOVED_DURING_ATTACH"
                        session.detached_at = now_iso()
                    else:
                        session.bridge = bridge
                        if session.attach_bridge is bridge:
                            session.attach_bridge = None
                        self._attach_bridges.pop(by_id, None)
                        self._attach_start_events.pop(by_id, None)
                        session.vtty_path = bridge.vtty_path
                        session.attached_real_path = real_path
                        session.bridge_generation += 1
                        session._push_probe_gate(bridge)
                        session._push_tx_gate(bridge)
                        session.retained_consoles = None
                        if ok:
                            session.state = "READY"
                            # #139：READY 即解除 boot quiet gate（詳見 _probe_existing_bridge 註解）。
                            # #162：READY 確認點——confirm_ready 同步清 pending。
                            session.confirm_ready()
                            session.last_error = None
                            session.last_ready_at = now_iso()
                            session.recovering = False
                            session.recovery_started_at = None
                            session.pending_auto_login = False
                            self._reset_reprobe_progress_locked(session)
                            notify_ready = True
                        else:
                            session.state = "ATTACHED"
                            session.last_error = err
                            session.last_error_detail = err_detail
                            session.recovering = False
                            session.recovery_started_at = None
                            if profile.platform == "passthrough":
                                session.pending_auto_login = False
                                session.reset_passthrough_boot_gate()
                        self._restore_retained_human_console_locked(session)
            if cleanup_job is not None:
                receipt = self._finish_attach_bridge_stop(cleanup_job)
                if cleanup_reason == "removed":
                    with self._lock:
                        self._store_retained_consoles_locked(
                            session,
                            getattr(receipt, "preserved", None),
                            human_owner=session.retained_human_owner,
                            human_timeout_s=session.retained_human_timeout_s,
                        )
                return
            if notify_ready:
                self._on_ready(session.session_id)
        except Exception as exc:
            # 開埠失敗跳過（#84 PORT-4 閒置 COM 接管/被佔用跳過）：
            # bridge.start() 拋出例外（如 PermissionError / OSError — 埠被其他程式佔用）時，
            # session 退回 DETACHED 並記錄 ATTACH_FAILED:<ExcType>，不觸碰 RELEASED 狀態。
            # 此次 attach thread 結束後，_attach_inflight 由 _spawn_attach._run finally 清除，
            # 下次可透過手動 attach_device / clear_session 或裝置重新掃到（added）觸發重試。
            with self._lock:
                pending_job = session._pending_drain
                if pending_job is None:
                    candidate = self._draining_by_ids.get(by_id)
                    if candidate is not None and candidate.bridge is bridge:
                        pending_job = candidate
                released = session.state == "RELEASED" or by_id in self._released_by_ids
                bridge_still_attaching = self._attach_bridges.get(by_id) is bridge
            if pending_job is not None:
                if pending_job.kind == "attach_cleanup":
                    self._finish_attach_bridge_stop(pending_job)
                return
            if released and not bridge_still_attaching:
                return
            try:
                receipt = self._track_attach_bridge_stop(session, by_id, bridge)
                preserved = getattr(receipt, "preserved", None)
            except Exception:
                preserved = None
            with self._lock:
                released = session.state == "RELEASED" or by_id in self._released_by_ids
                if not released:
                    self._store_retained_consoles_locked(
                        session,
                        preserved,
                        human_owner=session.retained_human_owner,
                        human_timeout_s=session.retained_human_timeout_s,
                    )
                    session.state = "DETACHED"
                    session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                    session.detached_at = now_iso()
                    session.bridge = None
                    session.attached_real_path = None
            if not released:
                self._on_detached(session.session_id)

    def _template_by_name(self, name: str) -> ProfileTemplate | None:
        for t in self._templates:
            if t.profile_name == name:
                return t
        return None

    def _maybe_persist_sticky(self, *, by_id: str, profile_name: str,
                              source: str, real_path: str) -> None:
        """達 READY 的正向偵測才寫 sticky（#95）。TOCTOU：real_path 須與 attach 當時一致。
        須在 self._lock 內呼叫。"""
        if source != "detected":
            return
        cur = self._devices.get(by_id)
        if cur is None or cur.real_path != real_path:
            return
        if self._profile_detected.get(by_id) == profile_name:
            return
        self._profile_detected[by_id] = profile_name
        self._save_state()

    def _default_passthrough_template(self) -> ProfileTemplate | None:
        """auto-detect 失敗時的通用 passthrough fallback。

        優先選非 command-capable 的 passthrough（如 others-template，純 console），
        避免 uboot-template 這類具 ready_probe 的特定 passthrough 被誤當通用 fallback；
        若沒有非 command-capable 的 passthrough，才退而用任一 passthrough（向後相容）。
        """
        generic = next(
            (t for t in self._templates
             if t.platform == "passthrough" and not t.command_capable),
            None,
        )
        if generic is not None:
            return generic
        return next((t for t in self._templates if t.platform == "passthrough"), None)

    def _rematerialize_profile_locked(
        self, session: SessionRuntime, tpl: ProfileTemplate, source: str
    ) -> bool:
        """把既有 session 的 profile 換成另一個 template（#181）。

        須在 ``self._lock`` 內、且 ``session.bridge is None`` 時呼叫。保留 ``com``／
        ``act_no``／``device_by_id``；alias 只在仍是「舊 template 自動產生」的
        ``<profile_name>+<act_no>`` 形式時跟著改名，使用者自訂過的一律保留。
        ``session_id`` 依新 profile_name 重算（``_session_from_template`` 的
        ``f"{profile_name}:{com}"`` 規則），並同步搬移所有以 session_id 為 key 的結構。

        刻意**原地 mutate** ``session`` 物件而非重建：``retained_consoles``（保留給
        human minicom 的 PTY）、boot quiet 狀態等執行期欄位都掛在這個物件上，重建會把
        正掛著的 human console 一併丟掉——那正是 #182 抱怨的代價。

        回傳是否真的換過。新 session_id 已被別的 session 佔用時不動作（回 ``False``），
        避免把兩個 session 併成一個。
        """
        old_sid = session.session_id
        old_profile = session.profile
        new_sid = f"{tpl.profile_name}:{old_profile.com}"
        if new_sid != old_sid and new_sid in self._sessions:
            return False
        auto_alias = f"{old_profile.profile_name}+{old_profile.act_no}"
        alias = (
            f"{tpl.profile_name}+{old_profile.act_no}"
            if old_profile.alias == auto_alias
            else old_profile.alias
        )
        session.profile = dataclasses.replace(
            old_profile,
            profile_name=tpl.profile_name,
            alias=alias,
            platform=tpl.platform,
            prompt_regex=tpl.prompt_regex,
            login_regex=tpl.login_regex,
            password_regex=tpl.password_regex,
            post_login_cmd=tpl.post_login_cmd,
            ready_probe=tpl.ready_probe,
            username=tpl.username,
            user_env=tpl.user_env,
            pass_env=tpl.pass_env,
            env_file=tpl.env_file,
            console_unlock_env=tpl.console_unlock_env,
            console_unlock_idle_s=tpl.console_unlock_idle_s,
            timeout_s=tpl.timeout_s,
            quiet_window_s=tpl.quiet_window_s,
            hard_timeout_s=tpl.hard_timeout_s,
            log_dir=tpl.log_dir,
            max_console_line_chars=tpl.max_console_line_chars,
            bootloader_prompts=tpl.bootloader_prompts,
            boot_done_regex=tpl.boot_done_regex,
            uart=tpl.uart,
        )
        session.profile_source = source
        self._reset_reprobe_progress_locked(session)
        if new_sid != old_sid:
            session.session_id = new_sid
            # 以 comprehension 重建而非 pop+insert：保留 _sessions 的插入順序（#186 的
            # exact-match tiebreak 與 DETACHED-rebind 候選排序都看得到這個順序）。
            self._sessions = {
                (new_sid if sid == old_sid else sid): row
                for sid, row in self._sessions.items()
            }
            if old_sid in self._binding_overrides:
                self._binding_overrides[new_sid] = self._binding_overrides.pop(old_sid)
            probe_lock = self._reprobe_probe_locks.pop(old_sid, None)
            if probe_lock is not None:
                self._reprobe_probe_locks[new_sid] = probe_lock
            if old_sid in self._loaded_released:
                self._loaded_released[new_sid] = self._loaded_released.pop(old_sid)
        if alias != old_profile.alias:
            self._aliases.unassign(old_profile.alias)
        self._aliases.set_for_session(session.session_id, alias)
        return True

    def _suggest_profile_from_rx(self, bridge: UARTBridge) -> str | None:
        """拿最近的 RX 比對所有非 passthrough template 的 ``prompt_regex``（#181）。

        判斷邏輯與 ``serialwrap profile test`` 同源——#181 的實測正是「把真實 prompt
        餵給 `profile test` 會命中 prpl-template，只是那個判斷從來沒被套用到這個
        session 上」。這裡把它接到 ``self_test`` 的診斷輸出，讓掉進 catch-all 的
        session 至少能被告知「你看起來是哪一種」。

        passthrough template 一律跳過：``others-template`` 的 ``prompt_regex`` 是
        ``.*``、恆真，建議回它等於沒建議。
        """
        if not self._templates:
            return None
        try:
            snapshot = clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))
        except Exception:
            return None
        if not snapshot:
            return None
        for tpl in self._templates:
            if tpl.platform == "passthrough":
                continue
            try:
                if re.search(tpl.prompt_regex, snapshot):
                    return tpl.profile_name
            except re.error:
                continue
        return None

    def _reresolve_profile_on_reattach(
        self,
        by_id: str,
        *,
        preopened_op: _AttachOp | None = None,
    ) -> bool:
        """既有 session 重新 attach 前重新解析 profile（#181）。

        #95 建立的四層優先序（pin > sticky > detect > fallback）只實作在
        ``_attach_by_id_dynamic``——也就是「該裝置**從未**建過 session」的路徑。既有
        session 走 ``_attach_by_id``，它以 ``device_by_id`` 找到 session 後就原封不動
        沿用 ``session.profile``；而 ``clear_session`` 只 detach bridge、**不刪 session
        物件**，於是「``session pin`` + ``session clear``」對既有 session 完全沒有效果。
        pin 的說明是「最高優先，繞過偵測」，是使用者唯一被告知的逃生口，實際卻是被
        接受、被持久化，然後被忽略（#181 問題 1）。本方法補上這條路徑。

        另外 ``fallback``（catch-all，如 ``others-template``）是**未經量測**的分類：
        attach 當下板子還在噴 log、沒有乾淨 prompt 就會掉進來，而它的空 ``ready_probe``
        使 ``command_capable`` 永久為 false，形成沒有出口的單向門（#181 問題 2/3）。
        它不具權威性，因此重新 attach 時再給一次偵測機會；``detected``／``sticky`` 是
        量測過的、``yaml-target`` 是人為宣告的，一律不動。

        偵測會送 ``\r``，故 boot quiet window（#130，U-Boot autoboot 保護）內不做；
        session 已有 bridge 時也不做（再開 PROBE bridge 會造成 two-reader）。
        """
        from .config import UartProfile

        if preopened_op is not None and preopened_op.cancelled.is_set():
            self._finish_attach_op(preopened_op)
            return False

        with self._lock:
            session = next(
                (s for s in self._sessions.values() if s.profile.device_by_id == by_id),
                None,
            )
            if session is None or session.profile_source == "yaml-target":
                return True
            if session.bridge is not None:
                return True
            if session.state == "RELEASED" or by_id in self._released_by_ids:
                return False
            pin_name = self._profile_pins.get(by_id)
            pinned_tpl = self._template_by_name(pin_name) if pin_name else None
            if pinned_tpl is not None:
                # 解析得到的 pin 為最高優先：改用它並繞過偵測（契約即「繞過偵測」）。
                # 已經是該 profile 時不必動作，但同樣不往下偵測。
                if pinned_tpl.profile_name != session.profile.profile_name:
                    if self._rematerialize_profile_locked(session, pinned_tpl, "pin"):
                        if preopened_op is not None:
                            preopened_op.session_id = session.session_id
                        self._save_state()
                return True
            # **無法解析的 pin**（指向已移除或改名的 profile）視同無 pin，繼續往下偵測——
            # 與 _attach_by_id_dynamic 的四層優先序一致（那裡 `tpl is None` 時同樣會落到
            # sticky/detect）。若只因「state.json 裡留著一個字串」就 return，fallback
            # session 會被一筆失效的 pin 永久卡在「不套用 pin、也不再偵測」的死角。
            if session.profile_source != "fallback" or not self._templates:
                return True
            if session.boot_quiet_active():
                return True
            dev = self._devices.get(by_id)
            if dev is None:
                return True
            real_path = dev.real_path
            preopened_op = self._active_attach_ops.get(by_id) or preopened_op

        # 以獨立 PROBE bridge 偵測，與 _attach_by_id_dynamic 對稱：此刻 session.bridge
        # 為 None（上面已確認），裝置未被本 daemon 開著。同一 by_id 的並發 attach 由
        # _spawn_attach 的 _attach_inflight 擋掉。
        probe_bridge = UARTBridge(
            "PROBE",
            real_path,
            UartProfile(),
            self._wal,
            preopened_port=preopened_op.port if preopened_op is not None else None,
            owns_port=False if preopened_op is not None else True,
        )
        if preopened_op is not None and not self._register_probe_bridge(preopened_op, probe_bridge):
            self._finish_attach_op(preopened_op)
            return False
        detected: ProfileTemplate | None = None
        try:
            probe_bridge.start()
            detected = detect_template(probe_bridge, self._templates)
        except Exception:
            detected = None
        finally:
            probe_stopped = self._stop_probe_bridge_safely(probe_bridge)
        if not probe_stopped:
            return False
        if preopened_op is not None:
            with self._lock:
                if preopened_op.probe_bridge is probe_bridge:
                    preopened_op.probe_bridge = None
                cancelled = preopened_op.cancelled.is_set()
            if cancelled:
                self._finish_attach_op(preopened_op)
                return False
        cancelled = False
        invalid = False
        with self._lock:
            # 釋鎖期間狀態可能已變（hotplug／release／另一條路徑改了 profile），重驗。
            session = next(
                (s for s in self._sessions.values() if s.profile.device_by_id == by_id),
                None,
            )
            device = self._devices.get(by_id)
            cancelled = preopened_op is not None and preopened_op.cancelled.is_set()
            invalid = bool(
                session is None
                or session.bridge is not None
                or device is None
                or device.real_path != real_path
                or cancelled
                or (session is not None and session.state == "RELEASED")
                or by_id in self._released_by_ids
                or by_id in self._draining_by_ids
                or self._shutting_down
            )
            if (
                not invalid
                and detected is not None
                and session is not None
                and session.profile_source == "fallback"
                and detected.profile_name != session.profile.profile_name
            ):
                self._rematerialize_profile_locked(session, detected, "detected")
                if preopened_op is not None:
                    preopened_op.session_id = session.session_id
                save_profile = True
            else:
                save_profile = False
            if save_profile:
                self._save_state()
        if invalid:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return False
        if detected is None:
            return True
        # sticky（_profile_detected）刻意不在此寫入：既有不變量是「達 READY 的正向偵測
        # 才寫 sticky」（_maybe_persist_sticky），profile_source 現已是 "detected"，
        # 後續 attach 流程走到 READY 時會照既有路徑寫入。
        return True

    def _attach_by_id_dynamic(
        self,
        by_id: str,
        *,
        preopened_op: _AttachOp | None = None,
    ) -> None:
        """動態偵測 template 並建立新 session。"""
        skip_attach = False
        with self._lock:
            if self._shutting_down or by_id in self._draining_by_ids:
                import logging
                logging.getLogger("serialwrap").info("shutdown 或 drain 中，略過動態 attach %s", by_id)
                skip_attach = True
                real_path = ""
            else:
                dev = self._devices.get(by_id)
                if dev is None:
                    skip_attach = True
                    real_path = ""
                else:
                    real_path = dev.real_path
                    preopened_op = self._active_attach_ops.get(by_id) or self._preopened_ports.get(by_id) or preopened_op
                    if preopened_op is not None and preopened_op.cancelled.is_set():
                        skip_attach = True
        if skip_attach:
            self._discard_preopened_port(by_id, preopened_op)
            return
        probe_real_path = real_path  # detect 當時的 real_path（#95 HIGH#2 TOCTOU：sticky 須比對此值）

        # 四層優先序（#95）：pin > sticky > detect > fallback。pin/sticky 命中跳過 probe。
        tpl: ProfileTemplate | None = None
        source: str | None = None
        pin_name = self._profile_pins.get(by_id)
        if pin_name:
            tpl = self._template_by_name(pin_name)
            if tpl is not None:
                source = "pin"
        if tpl is None:
            sticky_name = self._profile_detected.get(by_id)
            if sticky_name:
                tpl = self._template_by_name(sticky_name)
                if tpl is not None:
                    source = "sticky"

        if preopened_op is None:
            preopened_op, open_error = self._preopen_dynamic_attach_port(
                by_id,
                real_path,
                UartProfile(),
            )
            if open_error is not None:
                failure_template = tpl or self._default_passthrough_template()
                failure_source = source if tpl is not None else "fallback"
                self._record_dynamic_open_failure(
                    by_id,
                    real_path,
                    open_error,
                    template=failure_template,
                    source=failure_source,
                )
                return
            if preopened_op is None:
                return

        if tpl is None:
            default_uart = UartProfile()
            with self._lock:
                if (
                    self._shutting_down
                    or by_id in self._draining_by_ids
                    or (preopened_op is not None and preopened_op.cancelled.is_set())
                ):
                    skip_attach = True
                else:
                    skip_attach = False
            if skip_attach:
                self._discard_preopened_port(by_id, preopened_op)
                return
            probe_bridge = UARTBridge(
                "PROBE",
                real_path,
                default_uart,
                self._wal,
                preopened_port=preopened_op.port if preopened_op is not None else None,
                owns_port=False if preopened_op is not None else True,
            )
            if preopened_op is not None and not self._register_probe_bridge(preopened_op, probe_bridge):
                self._finish_attach_op(preopened_op)
                return
            detected: ProfileTemplate | None = None
            try:
                probe_bridge.start()
                # #130 註：detect_template 只在「全新動態裝置（尚無 session）」時執行，
                # 無既有 session 即無 boot quiet 狀態可循；既有 session 的 re-attach
                # 一律走 _attach_by_id（其 attach probe 已受 quiet gate 保護）。
                detected = detect_template(probe_bridge, self._templates)
            except Exception:
                pass
            finally:
                probe_stopped = self._stop_probe_bridge_safely(probe_bridge)
            if not probe_stopped:
                return
            if preopened_op is not None:
                with self._lock:
                    if preopened_op.probe_bridge is probe_bridge:
                        preopened_op.probe_bridge = None
                    cancelled = preopened_op.cancelled.is_set()
                if cancelled:
                    self._finish_attach_op(preopened_op)
                    return
            if detected is not None:
                tpl, source = detected, "detected"
        if tpl is None:
            tpl, source = self._default_passthrough_template(), "fallback"
        if tpl is None:
            self._discard_preopened_port(by_id, preopened_op)
            return

        skip_attach = False
        with self._lock:
            # 確認裝置仍在線且 session 數仍未超限
            if (
                self._shutting_down
                or by_id in self._draining_by_ids
                or by_id not in self._devices
                or len(self._sessions) >= self._max_sessions
                or (preopened_op is not None and preopened_op.cancelled.is_set())
            ):
                skip_attach = True
            else:
                session = self._session_from_template(tpl, by_id)
                session.profile_source = source
                if preopened_op is not None:
                    preopened_op.session_id = session.session_id
        if skip_attach:
            self._discard_preopened_port(by_id, preopened_op)
            return

        # 用正確 uart 參數重新開 bridge
        profile = session.profile
        skip_attach = False
        with self._lock:
            if self._shutting_down or by_id in self._draining_by_ids:
                skip_attach = True
            else:
                dev = self._devices.get(by_id)
                if dev is None or session.state == "RELEASED" or by_id in self._released_by_ids:
                    skip_attach = True
                else:
                    real_path = dev.real_path
                    session.state = "ATTACHING"
                    session.last_error = None
                    gen_before = session.bridge_generation
                    session_id = session.session_id
                    passthrough = profile.platform == "passthrough"
                    require_login = not passthrough and (
                        session.pending_auto_login or bool(profile.login_regex or profile.console_unlock_env)
                    )
                    # passthrough 一律不執行 attach probe 或登入流程。
                    command_capable = profile.command_capable and not passthrough
                preopened_op = self._active_attach_ops.get(by_id) or self._preopened_ports.get(by_id) or preopened_op
        if skip_attach:
            self._discard_preopened_port(by_id, preopened_op)
            return

        bridge = UARTBridge(
            profile.com,
            real_path,
            profile.uart,
            self._wal,
            on_console_line=lambda client_id, line, sid=session_id: self._on_bridge_console_line(sid, client_id, line),
            on_rx_data=lambda data, wal_seq=None, sid=session_id: self._on_bridge_rx(
                sid, data, wal_seq
            ),
            on_bridge_down=lambda reason, sid=session_id: self._handle_bridge_down(sid, bridge, reason),
            preopened_port=preopened_op.port if preopened_op is not None else None,
            owns_port=True,
        )

        attach_started = threading.Event()
        abort_attach = False
        with self._lock:
            if (
                self._shutting_down
                or by_id in self._draining_by_ids
                or session.state == "RELEASED"
                or by_id in self._released_by_ids
                or (preopened_op is not None and preopened_op.cancelled.is_set())
            ):
                abort_attach = True
            elif self._handoff_attach_op_locked(preopened_op):
                self._attach_bridges[by_id] = bridge
                self._attach_start_events[by_id] = attach_started
                session.attach_bridge = bridge
            else:
                abort_attach = True
        if abort_attach:
            if preopened_op is not None:
                self._finish_attach_op(preopened_op)
            return

        try:
            try:
                bridge.start()
            finally:
                attach_started.set()
            with self._lock:
                pending_job = self._draining_by_ids.get(by_id)
                if pending_job is None and self._shutting_down:
                    pending_job = self._begin_attach_bridge_stop_locked(
                        session,
                        by_id,
                        bridge,
                        preserve_consoles=True,
                        started_at=time.monotonic(),
                    )
            if pending_job is not None:
                if pending_job.kind == "attach_cleanup":
                    self._finish_attach_bridge_stop(pending_job)
                return
            # #150：probe 前採樣 raw RX 累計（dynamic 路徑無 boot-quiet gate）。
            rx_before = self._bridge_rx_total(bridge)
            with self._lock:
                probe_permit = (
                    self._begin_login_run_locked(session, bridge)
                    if command_capable
                    else None
                )
            probe_refused = False
            used_ensure_ready = False
            if not command_capable:
                ok, err = False, None
            else:
                try:
                    with self._system_permit_context(bridge, probe_permit):
                        if require_login:
                            auth, auth_res = resolve_session_auth(profile)
                            if self._credentials_declared_but_unresolved(profile, auth_res):
                                # 宣告帳密但解析空：不 probe/login，掛橋停 ATTACHED 終態（#140）。
                                self._emit_credentials_unresolved_warning(
                                    session_id, profile.com, auth_res
                                )
                                ok, err = False, ERROR_CREDENTIALS_UNRESOLVED
                            elif auth.console_unlock or (auth.username and auth.password):
                                used_ensure_ready = True
                                ok, err = ensure_ready(bridge, profile, auth=auth)
                            else:
                                ok, err = probe_ready(bridge, profile)
                        else:
                            ok, err = probe_ready(bridge, profile)
                except TxIncompleteError as exc:
                    if not _login_tx_refused(exc):
                        raise
                    ok, err = False, "PROMPT_UNAVAILABLE"
                    probe_refused = True
                if used_ensure_ready and not ok and not probe_refused:
                    # #150：於 bridge.stop() 前精煉。
                    err, err_detail = self._refine_probe_failure(session, bridge, err, rx_before)
                    self._track_attach_bridge_stop(session, by_id, bridge)
                    with self._lock:
                        session.state = "DETACHED"
                        session.last_error = err
                        session.last_error_detail = err_detail
                        session.detached_at = now_iso()
                        session.bridge = None
                        session.vtty_path = None
                        session.attached_real_path = None
                    self._on_detached(session.session_id)
                    return

            # #150：失敗才精煉（dynamic 路徑無 boot-quiet 合成錯，不需排除）。
            err_detail: str | None = None
            if not ok and not probe_refused and profile.platform != "passthrough":
                err, err_detail = self._refine_probe_failure(session, bridge, err, rx_before)

            notify_ready = False
            cleanup_job = None
            cleanup_reason = None
            with self._lock:
                # C1 backstop（dynamic 版）：release 落在飛行窗口內 → 關 FD、保留 RELEASED。
                if self._shutting_down or session.state == "RELEASED" or by_id in self._released_by_ids:
                    cleanup_reason = "released"
                    pending_job = self._draining_by_ids.get(by_id)
                    cleanup_job = pending_job if pending_job is not None and pending_job.bridge is bridge else None
                    if cleanup_job is None and self._attach_bridges.get(by_id) is bridge:
                        cleanup_job = self._begin_attach_bridge_stop_locked(
                            session,
                            by_id,
                            bridge,
                            preserve_consoles=False,
                            started_at=time.monotonic(),
                        )
                else:
                    current = self._devices.get(by_id)
                    if current is None or current.real_path != real_path or session.state == "DETACHED" or session.bridge_generation != gen_before:
                        cleanup_reason = "removed"
                        cleanup_job = self._begin_attach_bridge_stop_locked(
                            session,
                            by_id,
                            bridge,
                            preserve_consoles=True,
                            started_at=time.monotonic(),
                        )
                        session.state = "DETACHED"
                        session.last_error = "DEVICE_REMOVED_DURING_ATTACH"
                        session.detached_at = now_iso()
                    else:
                        session.bridge = bridge
                        if session.attach_bridge is bridge:
                            session.attach_bridge = None
                        self._attach_bridges.pop(by_id, None)
                        self._attach_start_events.pop(by_id, None)
                        session.vtty_path = bridge.vtty_path
                        session.attached_real_path = real_path
                        session.bridge_generation += 1
                        session._push_probe_gate(bridge)
                        session._push_tx_gate(bridge)
                        if ok:
                            session.state = "READY"
                            # #139：READY 即解除 boot quiet gate（詳見 _probe_existing_bridge 註解）。
                            # #162：READY 確認點——confirm_ready 同步清 pending。
                            session.confirm_ready()
                            session.last_error = None
                            session.last_ready_at = now_iso()
                            session.recovering = False
                            session.recovery_started_at = None
                            session.pending_auto_login = False
                            self._reset_reprobe_progress_locked(session)
                            self._maybe_persist_sticky(by_id=by_id, profile_name=profile.profile_name,
                                                       source=session.profile_source, real_path=probe_real_path)
                            notify_ready = True
                        else:
                            session.state = "ATTACHED"
                            session.last_error = err
                            session.last_error_detail = err_detail
                            session.recovering = False
                            session.recovery_started_at = None
                            if profile.platform == "passthrough":
                                session.pending_auto_login = False
                                session.reset_passthrough_boot_gate()
            if cleanup_job is not None:
                receipt = self._finish_attach_bridge_stop(cleanup_job)
                if cleanup_reason == "removed":
                    with self._lock:
                        self._store_retained_consoles_locked(
                            session,
                            getattr(receipt, "preserved", None),
                            human_owner=session.retained_human_owner,
                            human_timeout_s=session.retained_human_timeout_s,
                        )
                return
            if notify_ready:
                self._on_ready(session.session_id)
        except Exception as exc:
            # 動態路徑開埠失敗跳過（#84 PORT-4 閒置 COM 接管/被佔用跳過）：
            # bridge.start() 拋出例外（埠被佔、PermissionError 等）時，
            # 已建立的動態 session 退回 DETACHED，不污染 RELEASED，
            # 下次可透過手動 attach_device / clear_session 或裝置重新出現（added）觸發重試。
            with self._lock:
                pending_job = session._pending_drain
                if pending_job is None:
                    candidate = self._draining_by_ids.get(by_id)
                    if candidate is not None and candidate.bridge is bridge:
                        pending_job = candidate
                released = session.state == "RELEASED" or by_id in self._released_by_ids
                bridge_still_attaching = self._attach_bridges.get(by_id) is bridge
            if pending_job is not None:
                if pending_job.kind == "attach_cleanup":
                    self._finish_attach_bridge_stop(pending_job)
                return
            if released and not bridge_still_attaching:
                return
            try:
                self._track_attach_bridge_stop(session, by_id, bridge)
            except Exception:
                pass
            with self._lock:
                released = session.state == "RELEASED" or by_id in self._released_by_ids
                if not released:
                    session.state = "DETACHED"
                    session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                    session.detached_at = now_iso()
                    session.bridge = None
                    session.vtty_path = None
                    session.attached_real_path = None
            if not released:
                self._on_detached(session.session_id)

    def _last_prompt_start(self, text: str, prompt_regex: str) -> int | None:
        regex = re.compile(prompt_regex)
        last: re.Match[str] | None = None
        for match in regex.finditer(text):
            last = match
        return last.start() if last is not None else None

    def _extract_command_stdout(
        self, text: str, command: str, prompt_regex: str, *, completion_regex: str | None = None,
    ) -> str:
        cleaned = clean_text(text)
        prompt_start = self._last_prompt_start(cleaned, prompt_regex)
        # producer 完成模式下，前置 echo redraw／遲來舊 prompt 不能裁掉其後結果。
        # 仍使用 profile prompt 擷取，但只裁掉完成標記之後的尾 prompt。
        completion = re.search(completion_regex, text) if completion_regex is not None else None
        completion_end = len(clean_text(text[:completion.end()])) if completion is not None else None
        if prompt_start is not None and (
            completion_regex is None or (completion_end is not None and prompt_start >= completion_end)
        ):
            cleaned = cleaned[:prompt_start]
        lines = cleaned.lstrip("\n").splitlines()
        if command:
            command_stripped = command.strip()
            stripped = False
            while lines and lines[0].strip().endswith(command_stripped):
                lines = lines[1:]
                stripped = True
            if not stripped:
                original_lines = lines
                # 只把原始串流中 BusyBox 的雙 CR＋LF 視為折行證據；一般 stdout 換行不可拼接。
                if "\r\r\n" in text:
                    lines = _strip_wrapped_command_echo(lines, command_stripped)
                # bash readline 會在長回顯折行處寫入「空白＋CR」再接續同一行。
                # 只接受首個非空行中，原命令與至少相隔 20 字元的折行標記；
                # 不把 stdout 的任意空白或較短片段當成回顯。
                if lines is original_lines and " \r" in text:
                    match = _match_readline_wrapped_echo(text, command_stripped)
                    if match is not None:
                        start, end = match
                        candidate_text = text[:start] + text[start:end].replace(" \r", "") + text[end:]
                        candidate_cleaned = clean_text(candidate_text)
                        candidate_prompt_start = self._last_prompt_start(candidate_cleaned, prompt_regex)
                        candidate_completion = (
                            re.search(completion_regex, candidate_text) if completion_regex is not None else None
                        )
                        candidate_completion_end = (
                            len(clean_text(candidate_text[:candidate_completion.end()]))
                            if candidate_completion is not None else None
                        )
                        if candidate_prompt_start is not None and (
                            completion_regex is None
                            or (candidate_completion_end is not None
                                and candidate_prompt_start >= candidate_completion_end)
                        ):
                            candidate_cleaned = candidate_cleaned[:candidate_prompt_start]
                        candidate_lines = candidate_cleaned.lstrip("\n").splitlines()
                        if candidate_lines and candidate_lines[0].strip().endswith(command_stripped):
                            lines = candidate_lines[1:]
        return "\n".join(lines).strip("\n")

    def _open_interactive_locked(
        self,
        session: SessionRuntime,
        *,
        owner: str,
        timeout_s: float,
        recovery_mode: bool = False,
        suspended_human: bool = False,
    ) -> InteractiveLease:
        interactive_id = uuid.uuid4().hex
        lease = InteractiveLease(
            interactive_id=interactive_id,
            session_id=session.session_id,
            owner=owner,
            created_at=now_iso(),
            timeout_s=timeout_s,
            recovery_mode=recovery_mode,
            suspended_human=suspended_human,
        )
        self._interactive[interactive_id] = lease
        session.interactive_session_id = interactive_id
        if self._admission is not None:
            self._admission.set_flag(session.profile.com, "lease", True)
        assert session.bridge is not None
        session.bridge.set_interactive_owner(owner)
        session._push_tx_gate(session.bridge)
        return lease

    def _record_interactive_terminal_locked(
        self,
        lease: InteractiveLease,
        *,
        terminal_status: Literal["closed", "expired"],
        keep_tombstone: bool,
    ) -> None:
        """設定 lease 終態並維護每個 session 的有界 expired FIFO。"""
        lease.status = terminal_status
        if not keep_tombstone:
            self._interactive.pop(lease.interactive_id, None)
            return

        tombstones = self._tombstones.setdefault(lease.session_id, deque())
        if lease.interactive_id not in tombstones:
            tombstones.append(lease.interactive_id)
        while len(tombstones) > INTERACTIVE_TOMBSTONE_MAX:
            expired_id = tombstones.popleft()
            expired_lease = self._interactive.get(expired_id)
            if expired_lease is not None and expired_lease.status == "expired":
                self._interactive.pop(expired_id, None)
        if not tombstones:
            self._tombstones.pop(lease.session_id, None)

    def _close_interactive_locked(
        self,
        session: SessionRuntime,
        *,
        interactive_id: str | None = None,
        expected_owner: str | None = None,
        terminal_status: Literal["closed", "expired"] = "closed",
        keep_tombstone: bool = False,
    ) -> tuple[InteractiveLease | None, _PostCloseAction]:
        """Close interactive lease。回傳 (closed_lease, post_close_action)。

        呼叫者必須在 _lock 外呼叫 post_close_action.execute()。
        """
        post = _PostCloseAction()
        lease_id = interactive_id or session.interactive_session_id
        if lease_id is None:
            return None, post
        lease = self._interactive.get(lease_id)
        if lease is not None and expected_owner is not None and lease.owner != expected_owner:
            return None, post

        # 帶 suspended_human 的 lease（bootloader recovery 或 #53 soft preempt）關閉時 →
        # 還原 stash 的 human lease 或丟棄；不限 recovery_mode，soft preempt（recovery_mode=False）亦適用。
        if lease is not None and lease.suspended_human:
            stash = session._stashed_human_lease
            session._stashed_human_lease = None
            bridge = session.bridge
            post.bridge = bridge
            post.needs_resume = True

            restored = False
            if stash is not None and not stash.expired():
                client_id = stash.owner.split(":", 1)[1] if ":" in stash.owner else ""
                if bridge is not None and bridge.console_has_external_peer(client_id):
                    # 還原 human lease 狀態（lock 內 state mutation）
                    self._interactive[stash.interactive_id] = stash
                    session.interactive_session_id = stash.interactive_id
                    bridge.set_interactive_owner(stash.owner)
                    restored = True

            if not restored:
                # 丟棄 stash：清除 session 狀態，resume 後清除 ghost bridge owner
                session.interactive_session_id = None
                if bridge is not None:
                    bridge.set_interactive_owner(None)
                post.clear_owner_after_resume = True

            if self._admission is not None:
                self._admission.set_flag(
                    session.profile.com,
                    "lease",
                    session.interactive_session_id is not None,
                )

            session._push_tx_gate(bridge)

            if lease is not None:
                self._record_interactive_terminal_locked(
                    lease,
                    terminal_status=terminal_status,
                    keep_tombstone=keep_tombstone,
                )
            return lease, post

        # 一般 close
        session.interactive_session_id = None
        if session.bridge is not None:
            session.bridge.set_interactive_owner(None)
        if self._admission is not None:
            self._admission.set_flag(session.profile.com, "lease", False)
        session._push_tx_gate(session.bridge)
        if lease is not None:
            self._record_interactive_terminal_locked(
                lease,
                terminal_status=terminal_status,
                keep_tombstone=keep_tombstone,
            )
        return lease, post

    def _refresh_interactive_locked(
        self, session: SessionRuntime
    ) -> tuple[InteractiveLease | None, _PostCloseAction]:
        post = _PostCloseAction()
        lease_id = session.interactive_session_id
        if lease_id is None:
            return None, post
        lease = self._interactive.get(lease_id)
        if lease is None:
            _, post = self._close_interactive_locked(session, interactive_id=lease_id)
            return None, post
        if not lease.owner.startswith("human:") and lease.expired():
            _, post = self._close_interactive_locked(
                session,
                interactive_id=lease_id,
                terminal_status="expired",
                keep_tombstone=True,
            )
            restored_id = session.interactive_session_id
            restored = self._interactive.get(restored_id) if restored_id is not None else None
            return restored, post
        if session.bridge is None:
            _, post = self._close_interactive_locked(session, interactive_id=lease_id)
            return None, post
        if lease.owner.startswith("human:"):
            client_id = lease.owner.split(":", 1)[1]
            if not session.bridge.console_has_external_peer(client_id):
                # peer 不在：進入 peer-loss grace 窗，不立即拆 lease（防瞬時 flap 誤拆）。
                if lease.peer_lost_at is None:
                    lease.peer_lost_at = time.monotonic()
                    return lease, post  # grace 窗開始：暫不拆
                if time.monotonic() - lease.peer_lost_at <= _HUMAN_PEER_GRACE_S:
                    return lease, post  # 仍在 grace 窗內：繼續持有
                # 超過 grace 窗：真正拆 lease
                session.bridge.detach_console(client_id)
                session.vtty_path = session.bridge.vtty_path
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
            lease.peer_lost_at = None  # peer 在：清 grace 計時
            snapshot = session.bridge.snapshot()
            if snapshot.get("interactive_owner") != lease.owner:
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
        return lease, post

    def _lease_context(
        self, lease: InteractiveLease | None, *, bridge: UARTBridge | None = None
    ) -> dict[str, Any]:
        interactive_owner = lease.owner if lease is not None else None
        human_attached = bool(interactive_owner and interactive_owner.startswith("human:"))
        # human_active：human_attached 且最近一次真實鍵入仍在時間窗內（#53）。
        # 讓「人類已 attach 但長時間 idle」的 lease 不再被當成正在使用，避免誤擋
        # agent 行為；無 bridge / 從未鍵入 / 逾時皆為 False。
        human_active = False
        if human_attached and bridge is not None:
            last = bridge.snapshot().get("last_human_input_at")
            # last 在 production 僅為 None 或 float；以 isinstance 防呆，避免遇到
            # 非數值（如未設定 return_value 的 mock）時在 '<=' 比較炸掉。
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                if (time.monotonic() - last) <= HUMAN_ACTIVE_WINDOW_S:
                    human_active = True
        return {
            "interactive_owner": interactive_owner,
            "human_attached": human_attached,
            "human_active": human_active,
            "recovery_mode": bool(lease is not None and lease.recovery_mode),
        }

    def _transition_to_attached(self, session: SessionRuntime, *, reason: str) -> None:
        notify_not_ready = False
        with self._lock:
            if session._pending_drain is not None:
                return
            # 在途 command 的 timeout/recovery 可能在 flash endpoint 已把 session 移入 FLASHING、
            # 或 device handoff 釋放成 RELEASED 之後才跑到這；此時不得覆寫狀態，否則 FLASHING 被打回
            # ATTACHED → exit_flashing 提早 return、bridge 卡 flash 模式（#69 Finding round4）。
            if session.state in {"FLASHING", "RELEASED"}:
                return
            if session.state == "READY":
                notify_not_ready = True
            session.state = "ATTACHED"
            session.last_error = reason
            session.recovering = False
            session.recovery_started_at = None
            session.pending_auto_login = False
            if self._admission is not None:
                self._admission.set_flag(session.profile.com, "recovering", False)
        if notify_not_ready:
            self._on_detached(session.session_id)

    def _wait_for_human_interactive_release(
        self,
        session_id: str,
        *,
        timeout_s: float,
    ) -> tuple[bool, str | None]:
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            post = _PostCloseAction()
            outcome: tuple[bool, str | None] | None = None
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None or session.bridge is None or session.state != "READY":
                    return False, "SESSION_NOT_READY"
                if session.recovering:
                    return False, "SESSION_RECOVERING"
                lease, post = self._refresh_interactive_locked(session)
                if lease is None:
                    outcome = (True, None)
                elif not lease.owner.startswith("human:"):
                    outcome = (False, "SESSION_INTERACTIVE_BUSY")
            post.execute()
            if outcome is not None:
                return outcome
            time.sleep(0.05)

        return False, "SESSION_INTERACTIVE_BUSY"

    def _spawn_reboot_recovery(self, session_id: str, timeout_s: float) -> None:
        def _run() -> None:
            start = time.monotonic()
            deadline = start + timeout_s
            # #130：deadline 延伸上限＝一個完整 quiet window + 一輪 probe 預算。
            # 防 boot-loop 板反覆吐 banner 不斷延長 quiet window，使 recovery
            # 執行緒無限延命；達上限即走逾時收尾（session 留 ATTACHED，其後
            # 一切探測仍受 quiet gate 保護，不會打斷 autoboot）。
            deadline_cap = start + BOOT_QUIET_WINDOW_S + timeout_s
            while time.monotonic() < deadline:
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is None:
                        return
                    if self._shutting_down:
                        return
                    if session._pending_drain is not None:
                        drain_pending = True
                    else:
                        drain_pending = False
                    bridge = session.bridge
                    by_id = session.profile.device_by_id
                    quiet_until = session.boot_quiet_until
                    quiet_active = self._boot_quiet_blocks_probe(session)
                if drain_pending:
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
                    continue
                now = time.monotonic()
                if quiet_active:
                    # #130 review Finding 6（順修）：改呼叫 boot_quiet_active() 統一入口，
                    # 不再直接比較 boot_quiet_until 屬性（原始寫法與其他呼叫點各自重複
                    # 判斷邏輯，日後調整 quiet 語意時容易漏改此處）。
                    # boot quiet window 內不送任何 system probe（ensure_ready 的
                    # prompt probe 會打 "\n" 到 UART、打斷 U-Boot autoboot 倒數）。
                    # 純被動等 RX：window 由開機完成訊號（login/prompt）解除或逾時過期。
                    # deadline 需涵蓋 window 結束後至少一輪完整 probe（hard_timeout_s
                    # 60s < BOOT_QUIET_WINDOW_S 180s，不延伸會在 window 內就逾時收尾）。
                    deadline = min(max(deadline, quiet_until + timeout_s), deadline_cap)
                    time.sleep(min(1.0, quiet_until - now))
                    continue
                if bridge is not None:
                    credentials_terminal = False
                    try:
                        with self._lock:
                            current = self._sessions.get(session_id)
                            if (
                                current is None
                                or current.bridge is not bridge
                                or current._pending_drain is not None
                            ):
                                continue
                            if self._boot_quiet_blocks_probe(current):
                                continue
                            probe_permit = self._begin_login_run_locked(current, bridge)
                        with self._system_permit_context(bridge, probe_permit):
                            auth, auth_res = resolve_session_auth(session.profile)
                            if self._credentials_declared_but_unresolved(session.profile, auth_res):
                                # 宣告帳密但解析空：recovery 不送 ensure_ready 空 probe，記終態（#140）。
                                self._emit_credentials_unresolved_warning(
                                    session_id, session.profile.com, auth_res
                                )
                                ok, err = False, ERROR_CREDENTIALS_UNRESOLVED
                                credentials_terminal = True
                            else:
                                ok, err = ensure_ready(bridge, session.profile, auth=auth)
                    except TxIncompleteError as exc:
                        if _login_tx_refused(exc):
                            # quiet/replaced run：回到 loop，下一輪依期限被動等待或重取 permit。
                            continue
                        import logging
                        logging.getLogger("serialwrap").warning(
                            "reboot-recovery ensure_ready TX 例外（session=%s）", session_id, exc_info=True
                        )
                        ok, err = False, "RECOVERY_ERROR"
                    except Exception:  # noqa: BLE001 — login/probe 非預期例外不得殺死 worker（致 session 永卡 RECOVERING）（#79 STA-6）
                        import logging
                        logging.getLogger("serialwrap").warning(
                            "reboot-recovery ensure_ready 例外（session=%s），續迴圈待逾時收尾", session_id, exc_info=True
                        )
                        ok, err = False, "RECOVERY_ERROR"
                    if ok:
                        with self._lock:
                            session = self._sessions.get(session_id)
                            if session is None or session.bridge is not bridge or session._pending_drain is not None:
                                continue
                            # unlocked ensure_ready 期間 session 可能被搶進 FLASHING/RELEASED；不得覆寫（#69 r4）。
                            if session.state in {"FLASHING", "RELEASED"}:
                                return
                            session.state = "READY"
                            # #139：READY 即解除 boot quiet gate（詳見 _probe_existing_bridge 註解）。
                            # #162：READY 確認點——confirm_ready 同步清 pending。
                            session.confirm_ready()
                            session.last_error = None
                            session.last_ready_at = now_iso()
                            session.recovering = False
                            session.recovery_started_at = None
                            session.pending_auto_login = False
                            if self._admission is not None:
                                self._admission.set_flag(session.profile.com, "recovering", False)
                            self._reset_reprobe_progress_locked(session)
                        self._on_ready(session_id)
                        return
                    with self._lock:
                        session = self._sessions.get(session_id)
                        if session is None or session.bridge is not bridge or session._pending_drain is not None:
                            continue
                        session.last_error = err
                    if credentials_terminal:
                        # NIT-1（#140 review）：CREDENTIALS_UNRESOLVED 為已知終態，補帳密前
                        # 每輪重解析都必然再命中同一 gate（僅空轉 1s + env_file stat）。既知終態
                        # 即 break，交由迴圈後的逾時收尾把 session 收斂成 ATTACHED（last_error 已是
                        # CREDENTIALS_UNRESOLVED，收尾的 `or RECOVERY_TIMEOUT` 不覆寫）——與空轉到
                        # deadline 的終態完全一致，只是不再空轉。僅此終態 break；其餘 not-ok
                        # （PROMPT_TIMEOUT 等暫時性錯誤）續迴圈重試。
                        break
                elif by_id and by_id in self._devices:
                    self._spawn_attach(by_id)
                time.sleep(1.0)
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None:
                    return
                # 同理：recovery 逾時收尾不得覆寫已搶進的 FLASHING/RELEASED（#69 r4）。
                if session.state in {"FLASHING", "RELEASED"}:
                    return
                session.recovering = False
                session.recovery_started_at = None
                session.pending_auto_login = False
                if self._admission is not None:
                    self._admission.set_flag(session.profile.com, "recovering", False)
                if session.bridge is None:
                    session.state = "DETACHED"
                    session.last_error = "RECOVERY_TIMEOUT"
                else:
                    session.state = "ATTACHED"
                    session.last_error = session.last_error or "RECOVERY_TIMEOUT"

        threading.Thread(target=_run, name=f"serialwrap-reboot-{session_id}", daemon=True).start()

    def _set_terminal_capture_locked(
        self,
        session: SessionRuntime,
        *,
        cmd_id: str,
        chunks: list[str] | None = None,
        error_code: str | None = None,
    ) -> None:
        if not cmd_id:
            return
        capture = self._background.get(cmd_id)
        if capture is None:
            capture = BackgroundCapture(
                cmd_id=cmd_id,
                session_id=session.session_id,
                from_seq=self._wal.current_seq + 1,
                quiet_window_s=session.profile.quiet_window_s,
                created_at=now_iso(),
                last_seq=self._wal.current_seq,
            )
            self._background[cmd_id] = capture
            self._evict_background_locked()
            # 僅「先前未掛載」（如 line 模式逾時）才回填 chunks；background 模式已
            # 於命令送出前掛載並經 _on_bridge_rx 即時累積，此處收到的 chunks 是同
            # 一段 RX 的全量重讀，重複疊加會造成 result_tail 內容重複（#159 review）。
            if chunks:
                for c in chunks:
                    capture.add_chunk(c, BG_CAPTURE_MAX_BYTES)
        capture.last_seq = self._wal.current_seq
        capture.last_activity_mono = time.monotonic()
        capture.status = "error" if error_code else "done"
        capture.error_code = error_code

    def _handle_reboot_command(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        *,
        command: str,
        source: str,
        cmd_id: str,
        timeout_s: float,
        execution_mode: str,
        operation: _ForegroundOperation | None = None,
        trusted_human: bool | None = None,
        permit: TxPermit | None = None,
    ) -> dict[str, Any]:
        human = source.startswith("human:") if trusted_human is None else trusted_human
        prompt_regex = session.profile.prompt_regex
        pre_offset = bridge.rx_snapshot_len()
        # #130：收到 reboot 命令**當下**即設 boot quiet window，不等 banner——
        # 真板從 shutdown 訊息到 U-Boot banner 可能間隔數秒，且 U-Boot 可能吃到
        # banner 前緩衝的 bytes；等 banner 才靜默會讓第一發 probe 落入 autoboot 窗。
        # 若 target 其實沒重開（2s 內 prompt 回來），RX 解除檢查會立即清掉本視窗。
        with self._lock:
            if not self._foreground_operation_current_locked(session, bridge, operation):
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            invalidate_deferred = getattr(bridge, "invalidate_deferred", None)
            if callable(invalidate_deferred):
                invalidate_deferred("reboot")
            if permit is None:
                permit = TxPermit(
                    cmd_id,
                    session.bridge_generation,
                    "human_line" if human else "agent",
                )
        if session.profile.console_unlock_env:
            # 已重開的舊 root prompt 不可在 recovery 中被當成解鎖證據。
            bridge.clear_rx_buffer()
        self._mark_session_tx(session)
        # 先送出已授權的 reboot，再於同一個寫入序列鎖內 arm quiet。若 RX 在
        # permit 發放後、OS 提交前看到 banner，提交 fence 會先拒絕這筆命令；若
        # 寫入成功、部分完成或結果不明，quiet 都會在下一個 UART writer 取得鎖前生效。
        write_lock = getattr(bridge, "_write_lock", None)
        write_context = write_lock if write_lock is not None else contextlib.nullcontext()
        with write_context:
            try:
                self._send_command_with_permit(
                    bridge,
                    command,
                    source=source,
                    cmd_id=cmd_id,
                    permit=permit,
                )
            except BaseException as exc:
                tx_result = getattr(exc, "result", None)
                if tx_result is not None and (
                    tx_result.confirmed > 0 or tx_result.outcome == "unknown"
                ):
                    session.arm_boot_quiet()
                raise
            else:
                session.arm_boot_quiet()
        if bridge.wait_for_regex_from(prompt_regex, pre_offset, min(timeout_s, 2.0)):
            # #139（belt-and-suspenders）：2s 內 prompt 回來，RX 解除檢查
            # （_update_boot_quiet_locked）通常已清 quiet，但 prompt 被 RX chunk 切割
            # miss 時，若不補清，READY session 會白白被 TX 靜默擋到 180s 過期。
            #
            # #162 回歸修：此處**只清 TX 靜默維度（clear_boot_quiet），不 confirm_ready**。
            # prpl/OpenWrt 的 `reboot` 是**非同步**的——shell 立刻重印 prompt（實機
            # WAL：TX `reboot\n` 後 11ms 就收到 `reboot\r\nroot@prplOS:/# `）但板子確實
            # 在重開。把它當成「READY 已再確認」是假陰性：pending 被清掉後，agent 的
            # 下一個命令會在 stale-READY 窗被放行、落進正在 shutdown 的系統，逾時後
            # 升級成 CTRL-C/CTRL-D 強制按鍵，正好打在 U-Boot autoboot 倒數上。
            # 保留 pending → 後續真 banner 抵達時 gate 已是關的，且 agent 命令要等
            # 確認 probe（nonce 實證）成功才放行。
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                session.clear_boot_quiet()
            raw_text = bridge.rx_text_from(pre_offset)
            stdout = self._extract_command_stdout(raw_text, command, prompt_regex)
            return {
                "ok": True,
                "execution_mode": execution_mode,
                "stdout": stdout,
                "partial": False,
            }

        if human:
            post = _PostCloseAction()
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                if session.session_id in self._interactive_post_close:
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
                lease, post = self._refresh_interactive_locked(session)
                if lease is None:
                    lease = self._open_interactive_locked(
                        session,
                        owner=source,
                        timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
                    )
                session.state = "ATTACHED"
                session.last_error = "REBOOTING"
                session.recovering = False
                session.recovery_started_at = None
                session.pending_auto_login = False
            post.execute()
            self._on_detached(session.session_id)
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "status": "interactive",
                "stdout": "",
                "partial": True,
                "recovery_action": "PROMOTE_HUMAN_INTERACTIVE",
            }

        with self._lock:
            if not self._foreground_operation_current_locked(session, bridge, operation):
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            session.pending_auto_login = True
            session.recovering = True
            session.recovery_started_at = now_iso()
            session.state = "RECOVERING"
            if self._admission is not None:
                self._admission.set_flag(session.profile.com, "recovering", True)
            session.last_error = None
        self._on_detached(session.session_id)
        self._spawn_reboot_recovery(session.session_id, session.profile.hard_timeout_s)
        return {
            "ok": True,
            "execution_mode": execution_mode,
            "stdout": "",
            "partial": True,
            "status": "recovering",
            "recovery_action": "EXPECT_REBOOT",
        }

    def execute_command(
        self,
        session_id: str,
        command: str,
        source: str,
        cmd_id: str,
        *,
        timeout_s: float = 10.0,
        mode: str = "line",
        expected_duration_s: float | None = None,
        completion_regex: str | None = None,
        trusted_human: bool | None = None,
    ) -> dict[str, Any]:
        if not valid_completion_regex(completion_regex):
            return {"ok": False, "error_code": ERROR_INVALID_COMPLETION_REGEX}
        normalized_mode = {"fg": "line", "bg": "background"}.get(mode, mode)
        human = source.startswith("human:") if trusted_human is None else trusted_human
        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        operation: _ForegroundOperation | None = None
        command_permit: TxPermit | None = None
        interactive_permit: TxPermit | None = None
        with self._lock:
            if cmd_id in self._cancelled_tx_cmd_ids:
                self._cancelled_tx_cmd_ids.pop(cmd_id, None)
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            session = self._sessions.get(session_id)
            if session is not None and session.state == "FLASHING":
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com}
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            # #139 execute-time gate（第二層，堵 queue race）：submit 通過第一層後、worker
            # 執行前 banner 才到（命令已在 arbiter queue）的競態於此攔下。arbiter._worker
            # 會把本 error_code 寫回命令記錄（status=error），`cmd status` 可觀測；命令
            # 未送 UART、零副作用，READY 重新確認後可重送。只有受信任的內部 human console
            # 命令可略過此 gate；RPC 的 source 字串不能授權豁免。
            # #162：改判 _agent_gate_reject_locked()——quiet 過期不再解除 gate，須等
            # READY 經 nonce probe 再確認（confirm_ready）才放行；pending 逾時／逾次
            # 則改回不可重試的 READY_UNCONFIRMED。
            if not human:
                gate = self._agent_gate_reject_locked(session)
                if gate is not None:
                    return gate
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            if normalized_mode == "interactive" and (
                session.foreground_busy or session._foreground_operation is not None
            ):
                return {"ok": False, "error_code": "SESSION_BUSY", "selector": session.profile.com}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None and normalized_mode != "interactive":
                if not human and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {
                        "ok": False,
                        "error_code": "SESSION_INTERACTIVE_BUSY",
                        "interactive_session_id": session.interactive_session_id,
                    }
            if busy_result is None:
                operation, busy_result = self._begin_foreground_operation_locked(
                    session,
                    kind="command",
                    cmd_id=cmd_id,
                    expected_duration_s=expected_duration_s,
                )
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
            if operation is not None:
                command_permit = TxPermit(
                    cmd_id,
                    session.bridge_generation,
                    "human_line" if human else "agent",
                )

        human_suspended = False
        mapped_aborted = False
        try:
            post.execute()
            if busy_result is not None:
                return busy_result
            if operation is not None:
                try:
                    from .arbiter import CommandArbiter

                    CommandArbiter.register_current_cancel_callback(
                        cmd_id, self.revoke_tx_op
                    )
                except Exception:  # noqa: BLE001 — direct manager calls沒有 arbiter worker
                    pass
            if suspend_human_interactive:
                bridge.suspend_interactive()
                human_suspended = True
            set_active = getattr(bridge, "tx_set_active", None)
            if callable(set_active):
                set_active(cmd_id)
            result = self._execute_command_inner(
                session, bridge, command, source, cmd_id,
                timeout_s=timeout_s, normalized_mode=normalized_mode,
                prompt_regex=prompt_regex,
                expected_duration_s=expected_duration_s,
                completion_regex=completion_regex,
                operation=operation,
                trusted_human=human,
                command_permit=command_permit,
            )
            with self._lock:
                aborted_by_drain = cmd_id in self._aborted_cmd_ids
            if aborted_by_drain and not result.get("ok", False):
                mapped_aborted = True
                return self._map_aborted_result(
                    cmd_id,
                    result,
                    bridge=bridge,
                    aborted_by_drain=True,
                )
            return result
        except BaseException as exc:
            with self._lock:
                aborted_by_drain = cmd_id in self._aborted_cmd_ids
            if aborted_by_drain or isinstance(exc, TxIncompleteError):
                mapped_aborted = aborted_by_drain
                return self._map_aborted_result(
                    cmd_id,
                    exc,
                    bridge=bridge,
                    aborted_by_drain=aborted_by_drain,
                )
            raise
        finally:
            try:
                if human_suspended:
                    bridge.resume_interactive()
            finally:
                try:
                    if operation is not None:
                        try:
                            if not mapped_aborted:
                                settle = getattr(bridge, "tx_settle", None)
                                if callable(settle):
                                    settle(cmd_id)
                        finally:
                            set_active = getattr(bridge, "tx_set_active", None)
                            if callable(set_active):
                                set_active(None)
                finally:
                    try:
                        if operation is not None:
                            try:
                                from .arbiter import CommandArbiter

                                CommandArbiter.unregister_current_cancel_callback(cmd_id)
                            except Exception:  # noqa: BLE001 — direct manager calls沒有 arbiter worker
                                pass
                        self._finish_foreground_operation(session, operation)
                    finally:
                        with self._lock:
                            self._aborted_cmd_ids.discard(cmd_id)

    def _append_aborted_in_flight_meta(
        self,
        bridge: UARTBridge,
        cmd_id: str,
        confirmed: int,
        tx_result: TxResult | None,
        *,
        unknown: bool,
    ) -> None:
        """BC-03：已確認寫出 ≥1 byte 後中止時寫 command_aborted_in_flight META。

        一般命令（``_map_aborted_result``）與 file.push／file.pull（OD-62）共用，確保欄位與
        寫入時機相同；file.* 的 ``cmd_id`` 為該次傳輸的 cmd_id 前綴、``confirmed`` 為整段累計。
        """
        if unknown:
            tx_outcome = "unknown"
        elif tx_result is not None and (
            tx_result.outcome != "completed" or tx_result.confirmed < tx_result.requested
        ):
            tx_outcome = "partial"
        else:
            tx_outcome = "complete"
        self._wal.append(
            com=bridge.com,
            direction="META",
            source="system",
            cmd_id=cmd_id,
            payload=b"",
            meta={
                "event": "command_aborted_in_flight",
                "confirmed_tx_bytes": confirmed,
                "tx_outcome": tx_outcome,
            },
        )

    def _map_aborted_result(
        self,
        cmd_id: str,
        result: dict[str, Any] | BaseException,
        *,
        bridge: UARTBridge,
        aborted_by_drain: bool,
    ) -> dict[str, Any]:
        """依 T06 的 TX 帳本把 drain 中止結果映射成可重試狀態碼。"""
        account = getattr(bridge, "tx_account", None)
        confirmed, unknown = account(cmd_id) if callable(account) else (0, False)
        tx_result = result.result if isinstance(result, TxIncompleteError) else None
        unknown = unknown or isinstance(result, TxUnknownError) or (
            tx_result is not None and tx_result.outcome == "unknown"
        )
        if confirmed > 0:
            self._append_aborted_in_flight_meta(
                bridge, cmd_id, confirmed, tx_result, unknown=unknown
            )
            return {"ok": False, "error_code": "COMMAND_ABORTED_IN_FLIGHT"}
        if unknown:
            return {"ok": False, "error_code": "SEND_FAILED"}
        if isinstance(result, TxIncompleteError):
            reason = (result.result.error or "").removeprefix("revoked:")
            if reason == "quiet":
                return {"ok": False, "error_code": ERROR_AUTOBOOT_QUIET}
            if reason in {"cancelled", "stale_generation", "draining"}:
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
        if aborted_by_drain:
            return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
        return {"ok": False, "error_code": "SEND_FAILED"}

    def _execute_command_inner(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        command: str,
        source: str,
        cmd_id: str,
        *,
        timeout_s: float,
        normalized_mode: str,
        prompt_regex: str,
        expected_duration_s: float | None = None,
        completion_regex: str | None = None,
        operation: _ForegroundOperation | None = None,
        trusted_human: bool | None = None,
        command_permit: TxPermit | None = None,
    ) -> dict[str, Any]:
        human = source.startswith("human:") if trusted_human is None else trusted_human
        if normalized_mode == "interactive":
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                if session.session_id in self._interactive_post_close:
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
                for bg_cmd_id in list(session.background_cmd_ids):
                    capture = self._background.get(bg_cmd_id)
                    if capture is not None:
                        capture.status = "done"
                lease = self._open_interactive_locked(session, owner=source, timeout_s=max(timeout_s, session.profile.hard_timeout_s))
                # S1X-a-1：op_id 必須與 _open_interactive_locked 經 _push_tx_gate 發佈的
                # lease owner（interactive:<lease_id>）一致，否則提交點必以 lease 拒絕。
                interactive_permit = TxPermit(
                    f"interactive:{lease.interactive_id}",
                    session.bridge_generation,
                    "interactive",
                    owner_gen=getattr(bridge, "owner_gen", 0),
                    cancel_op_id=cmd_id,
                )
            if command:
                self._mark_session_tx(session)
                try:
                    # 取消以原 cmd_id 撤銷；permit 的 cancel_op_id 讓提交點（fence 內）同時
                    # 檢查 cmd_id 與 lease op，不論取消落在 lease 開啟前或送出途中都零 TX。
                    self._send_command_with_permit(
                        bridge,
                        command,
                        source=source,
                        cmd_id=cmd_id,
                        permit=interactive_permit,
                    )
                except BaseException:
                    # 送出拋例外或被拒：關閉剛開的 lease，避免遺留 lease 讓後續命令
                    # 拿到 SESSION_INTERACTIVE_BUSY 直到逾時；例外交由 execute_command 映射。
                    close_post = _PostCloseAction()
                    with self._lock:
                        if session.interactive_session_id == lease.interactive_id:
                            _, close_post = self._close_interactive_locked(
                                session,
                                interactive_id=lease.interactive_id,
                            )
                    close_post.execute()
                    raise
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "stdout": "",
                "status": "interactive",
            }

        with self._lock:
            if not self._foreground_operation_current_locked(session, bridge, operation):
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            if normalized_mode != "background":
                for bg_cmd_id in list(session.background_cmd_ids):
                    capture = self._background.get(bg_cmd_id)
                    if capture is not None:
                        capture.status = "done"
        passthrough = session.profile.platform == "passthrough"
        if not passthrough and _is_reboot_command(command):
            return self._handle_reboot_command(
                session,
                bridge,
                command=command,
                source=source,
                cmd_id=cmd_id,
                timeout_s=timeout_s,
                execution_mode=normalized_mode,
                operation=operation,
                trusted_human=human,
                permit=command_permit,
            )
        completion_pattern = (
            _passthrough_completion_pattern(session.profile, completion_regex)
            if passthrough
            else completion_regex or prompt_regex
        )
        pre_offset = bridge.rx_snapshot_len()
        if completion_regex is not None:
            self._wal.append(
                com=session.profile.com, direction="META", source="system", cmd_id=cmd_id,
                payload=b"", meta={"event": "command_completion", "completion_regex": completion_regex},
            )
        if normalized_mode == "background":
            # #159：capture 必須在命令送出「之前」掛好——快速完成的命令會在
            # prompt 比對成功前就把全部輸出送完，若等 matched 之後才回溯建立
            # capture，這段輸出永遠不會經過 add_chunk()（result_tail 拿到空
            # chunks 卻回 lost:False 的假保證）。配合 _on_bridge_rx 對應修改
            # （background capture 不再被 foreground_busy 擋住），此後全程
            # （含下方等待迴圈與 CTRL_C 復原）RX 都會即時進 add_chunk()。
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                capture = BackgroundCapture(
                    cmd_id=cmd_id,
                    session_id=session.session_id,
                    from_seq=self._wal.current_seq + 1,
                    quiet_window_s=session.profile.quiet_window_s,
                    created_at=now_iso(),
                    last_seq=self._wal.current_seq,
                )
                self._background[cmd_id] = capture
                self._evict_background_locked()
                session.background_cmd_ids.append(cmd_id)
        self._mark_session_tx(session)
        assert command_permit is not None
        self._send_command_with_permit(
            bridge,
            command,
            source=source,
            cmd_id=cmd_id,
            permit=command_permit,
        )

        # — heartbeat / keepalive 迴圈 —
        effective_timeout = timeout_s
        if expected_duration_s is not None:
            effective_timeout = max(timeout_s, expected_duration_s)
        silence_limit = min(timeout_s, 30.0)
        deadline = time.monotonic() + effective_timeout
        matched = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_chunk = min(silence_limit, remaining)
            pre_rx = bridge.rx_snapshot_len()
            wait_pattern = completion_pattern if completion_pattern is not None else r"(?!)"
            if bridge.wait_for_regex_from(wait_pattern, pre_offset, wait_chunk):
                matched = True
                break
            if bridge.rx_snapshot_len() == pre_rx:
                # 真正靜默，不再等待
                break
            # 有 RX 活動，繼續等待

        if not matched:
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            return self._recover_after_failure(
                session,
                bridge,
                cmd_id=cmd_id,
                timeout_s=timeout_s,
                source=source,
                command=command,
                prompt_regex=prompt_regex,
                pre_offset=pre_offset,
                operation=operation,
                trusted_human=human,
            )
        raw_text = bridge.rx_text_from(pre_offset)
        if (
            passthrough
            and completion_regex is not None
            and not valid_completion_regex(prompt_regex)
        ):
            completion_match = re.search(completion_regex, raw_text)
            stdout = clean_text(
                raw_text[:completion_match.end()] if completion_match is not None else raw_text
            )
        else:
            stdout = self._extract_command_stdout(
                raw_text, command, prompt_regex, completion_regex=completion_regex
            )
        result: dict[str, Any] = {
            "ok": True,
            "execution_mode": normalized_mode,
            "stdout": stdout,
            "partial": False,
        }
        if normalized_mode == "background":
            # capture 已於命令送出前掛好並即時累積（#159），此處僅回填回應欄位。
            result["background_capture_id"] = cmd_id
        return result

    def _recover_after_failure(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        *,
        cmd_id: str,
        timeout_s: float,
        source: str,
        command: str,
        prompt_regex: str,
        pre_offset: int,
        operation: _ForegroundOperation | None = None,
        trusted_human: bool | None = None,
    ) -> dict[str, Any]:
        human = source.startswith("human:") if trusted_human is None else trusted_human
        with self._lock:
            if not self._foreground_operation_current_locked(session, bridge, operation):
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
        if human:
            post = _PostCloseAction()
            with self._lock:
                if session.session_id in self._interactive_post_close:
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
                lease, post = self._refresh_interactive_locked(session)
                if lease is None:
                    lease = self._open_interactive_locked(
                        session,
                        owner=source,
                        timeout_s=max(timeout_s, session.profile.hard_timeout_s),
                    )
            post.execute()
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "status": "interactive",
                "stdout": "",
                "partial": True,
                "recovery_action": "PROMOTE_HUMAN_INTERACTIVE",
            }

        if session.profile.platform == "passthrough":
            partial_stdout = clean_text(bridge.rx_text_from(pre_offset))
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                self._set_terminal_capture_locked(
                    session,
                    cmd_id=cmd_id,
                    chunks=[partial_stdout] if partial_stdout else None,
                    error_code="PROMPT_TIMEOUT",
                )
            return {
                "ok": False,
                "error_code": "PROMPT_TIMEOUT",
                "partial": True,
                "recovery_action": "NONE",
            }

        # #130 review Finding 1c（必修）：boot quiet window 內不送 CTRL_C/CTRL_D——這兩個
        # byte 跟 probe 一樣會打斷 U-Boot autoboot 倒數，且是最容易觸發的路徑（agent 正常
        # 送命令途中 target 自發重開機、逾時進入本函式）。跳過強制按鍵，直接落到下方既有
        # timeout 收尾（session 轉 ATTACHED），讓 autoboot 在無人打擾下自然跑完；window 解
        # 除後由既有 reprobe / reboot recovery 接手升 READY。
        # 迴圈外快速早退（省掉整段設置）；真正的授權判定在迴圈**內**逐 byte 重驗。
        if not session.boot_quiet_active():
            invalidate_deferred = getattr(bridge, "invalidate_deferred", None)
            if callable(invalidate_deferred):
                invalidate_deferred("recover")
            for action_name, payload in (("CTRL_C", b"\x03"), ("CTRL_D", b"\x04")):
                # #162 回歸修（實機事故 2026-07-31）：gate 必須逐 byte 重驗。第二個 byte
                # （CTRL-D）在第一個之後最多 min(timeout_s, 2.0) 秒才寫出，而 U-Boot
                # autoboot 倒數窗實測就是 3 秒——CTRL-C 送出後 banner 才抵達、quiet 於
                # 兩次寫入之間 arm 的情境下，舊碼（gate 只在迴圈外評估一次）會照送
                # CTRL-D 打斷 autoboot，把板子留在 `=> ` 一路卡到人工介入。
                # WAL 物證：seq=87 `\x03`(23:39:11.414) → seq=94 banner arm(23:39:11.948)
                # → seq=103 `\x04`(23:39:13.424，armed 後 1.48s) → seq=104 `=> `。
                with self._lock:
                    if not self._foreground_operation_current_locked(session, bridge, operation):
                        return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                    if session.boot_quiet_active():
                        break
                    permit = TxPermit(
                        "system:recover",
                        session.bridge_generation,
                        "system_probe",
                    )
                offset = bridge.rx_snapshot_len()
                self._mark_session_tx(session)
                try:
                    self._send_bytes_with_permit(
                        bridge,
                        payload,
                        source="system:recover",
                        cmd_id=None,
                        permit=permit,
                    )
                except TxIncompleteError as exc:
                    reason = (exc.result.error or "").removeprefix("revoked:")
                    if reason == "quiet":
                        break
                    if reason in {"draining", "cancelled", "stale_generation"}:
                        return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                    raise
                if bridge.wait_for_regex_from(prompt_regex, offset, min(timeout_s, 2.0)):
                    stdout = self._extract_command_stdout(bridge.rx_text_from(pre_offset), command, prompt_regex)
                    with self._lock:
                        if not self._foreground_operation_current_locked(session, bridge, operation):
                            return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
                        self._set_terminal_capture_locked(
                            session,
                            cmd_id=cmd_id,
                            chunks=[stdout] if stdout else None,
                            error_code="PROMPT_TIMEOUT_RECOVERED",
                        )
                        self._reset_reprobe_progress_locked(session)
                    # #156：CTRL_C/CTRL_D 攔截成功時 session 全程停留 READY、不觸發 detach，
                    # 既有「detach 才 flush」路徑（_on_detached → arbiter.unregister_session，#128）
                    # 天生跳過本分支。顯式呼叫 flush callback 補上同一 FLUSHED_BY_RECOVERY 終態語意；
                    # 鎖外呼叫比照 _transition_to_attached→_on_detached 的既有慣例，
                    # 避免 SessionManager._lock 與 CommandArbiter._lock 巢狀。
                    if self._on_command_flush is not None:
                        self._on_command_flush(session.session_id, "FLUSHED_BY_RECOVERY")
                    return {
                        "ok": True,
                        "error_code": "PROMPT_TIMEOUT_RECOVERED",
                        "stdout": stdout,
                        "partial": True,
                        "recovery_action": action_name,
                    }

        partial_stdout = clean_text(bridge.rx_text_from(pre_offset))
        with self._lock:
            if not self._foreground_operation_current_locked(session, bridge, operation):
                return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            self._set_terminal_capture_locked(
                session,
                cmd_id=cmd_id,
                chunks=[partial_stdout] if partial_stdout else None,
                error_code="PROMPT_TIMEOUT",
            )
        self._transition_to_attached(session, reason="PROMPT_TIMEOUT")

        # Auto-fallback: try force clear + reattach when both CTRL_C/D fail
        if source == "system:recover":
            with self._lock:
                if not self._foreground_operation_current_locked(session, bridge, operation):
                    return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY"}
            force_result = self._force_recover(session)
            if force_result.get("recovered"):
                return {
                    "ok": True,
                    "error_code": "PROMPT_TIMEOUT_FORCE_RECOVERED",
                    "partial": True,
                    "recovery_action": "FORCE_CLEAR_REATTACH",
                    "session": force_result.get("session", {}),
                }

        return {
            "ok": False,
            "error_code": "PROMPT_TIMEOUT",
            "partial": True,
            "recovery_action": "NONE",
        }

    def get_session_state(self, selector: str) -> dict[str, Any]:
        session = self.get_session(selector)
        if session is None:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
        with self._lock:
            # #162 有界化：service 層 submit-time gate 讀的是這份快照，須在序列化前
            # 先評估 pending 是否已逾時／逾次落終態，否則 submit gate 會永遠看到
            # pending=True、永遠回可重試的 AUTOBOOT_QUIET。
            session.expire_ready_reconfirm()
            return {"ok": True, "session": session.to_public_dict()}

    def attach_console(self, selector: str, *, label: str | None = None) -> dict[str, Any]:
        post = _PostCloseAction()
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state not in {"READY", "ATTACHED"}:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            if session.session_id in self._interactive_post_close:
                return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "selector": session.profile.com}
            payload = session.bridge.attach_console(label=label)
            if payload.get("client_id") is None:
                # 無 PTY 平台（Windows TCP console，#84 PORT-2）：human 直接連 TCP 端點，console
                # client 於連線時自動建立、raw ownership 由 bridge 在 accept 時授予。此處**不可**用
                # owner=f"human:{client_id}"（會是 human:None）開 lease 覆寫之；只回傳端點即可（#84 review）。
                if session.vtty_path is None:
                    session.vtty_path = payload["vtty"]
                payload["session"] = session.to_public_dict()
                return {"ok": True, **payload}
            if session.vtty_path is None:
                session.vtty_path = payload["vtty"]
            lease, post = self._refresh_interactive_locked(session)
            if session.state in {"ATTACHED", "READY"} and lease is None:
                lease = self._open_interactive_locked(
                    session,
                    owner=f"human:{payload['client_id']}",
                    timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
                )
                payload["interactive_session_id"] = lease.interactive_id
                payload["interactive_owner"] = not (
                    session.foreground_busy or session._foreground_operation is not None
                )
            payload["session"] = session.to_public_dict()
            result = {"ok": True, **payload}
        post.execute()
        return result

    def detach_console(self, selector: str, client_id: str) -> dict[str, Any]:
        post = _PostCloseAction()
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            human_owner = f"human:{client_id}"
            lease, post = self._refresh_interactive_locked(session)
            ok = session.bridge.detach_console(client_id)
            if lease is not None and lease.owner == human_owner:
                # Human lease close：一般 human lease 的 suspended_human=False，故
                # _close_interactive_locked 走一般 close 分支、post.needs_resume 必為 False
                # （no-op post），安全丟棄。（還原分支現以 suspended_human 判定，非 recovery_mode。）
                _, _ = self._close_interactive_locked(
                    session, interactive_id=lease.interactive_id, expected_owner=human_owner
                )
            session.vtty_path = session.bridge.vtty_path
            if not ok:
                result = {"ok": False, "error_code": "CONSOLE_NOT_FOUND", "client_id": client_id}
            else:
                result = {"ok": True, "client_id": client_id, "session": session.to_public_dict()}
        post.execute()
        return result

    def list_consoles(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            return {"ok": True, "consoles": session.bridge.list_consoles(), "session": session.to_public_dict()}

    def interactive_open(
        self,
        selector: str,
        *,
        owner: str = "agent",
        timeout_s: float = 60.0,
        command: str = "",
        allow_attached: bool = False,
    ) -> dict[str, Any]:
        command_permit: TxPermit | None = None
        while True:
            retry_after_post = False
            command_permit = None
            post = _PostCloseAction()
            with self._lock:
                session = self.get_session(selector)
                if session is None:
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
                if session.state == "RECOVERING":
                    return {
                        "ok": False,
                        "error_code": "SESSION_RECOVERING",
                        "selector": session.profile.com,
                    }
                if session.state == "FLASHING":
                    return {
                        "ok": False,
                        "error_code": "FLASHING_BUSY",
                        "selector": session.profile.com,
                    }
                if session.bridge is None:
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                if session.state == "READY":
                    if session.foreground_busy or session._foreground_operation is not None:
                        return {"ok": False, "error_code": "SESSION_BUSY", "selector": session.profile.com}
                    if session.session_id in self._interactive_post_close:
                        return {
                            "ok": False,
                            "error_code": "SESSION_INTERACTIVE_BUSY",
                            "interactive_session_id": session.interactive_session_id,
                        }
                    # READY 一律走既有路徑；allow_attached 在 READY 下不改變語意。
                    existing, post = self._refresh_interactive_locked(session)
                    if post.needs_resume:
                        retry_after_post = True
                    elif existing is not None:
                        # #53 soft preempt：human lease 閒置（human_active=False）時，
                        # agent 可暫停（降級）human lease 取得控制權；human console 不中斷，
                        # 其鍵入進 deferred buffer，agent close 後還原並回放。
                        # human 仍 active 或既有為 agent lease → 維持 BUSY。
                        # 註：從未鍵入（last_human_input_at=None）視為 idle、可被 preempt——
                        # soft preempt 非破壞性（降級+回放），故此處理為刻意行為。
                        human_active = self._lease_context(
                            existing, bridge=session.bridge
                        )["human_active"]
                        if (
                            existing.owner.startswith("human:")
                            and not human_active
                            and not owner.startswith("human:")
                        ):
                            self._interactive.pop(existing.interactive_id, None)
                            session.interactive_session_id = None
                            session._stashed_human_lease = existing
                            session.bridge.suspend_interactive()
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=timeout_s,
                                suspended_human=True,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": False,
                                "soft_preempted": True,
                            }
                        else:
                            return {
                                "ok": False,
                                "error_code": "SESSION_INTERACTIVE_BUSY",
                                "interactive_session_id": session.interactive_session_id,
                            }
                    else:
                        lease = self._open_interactive_locked(session, owner=owner, timeout_s=timeout_s)
                        bridge = session.bridge
                        result: dict[str, Any] = {
                            "ok": True,
                            "interactive_id": lease.interactive_id,
                            "session": session.to_public_dict(),
                            "recovery_mode": False,
                        }

                elif not allow_attached:
                    # 原有行為：未 opt-in 時只允許 READY。
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                elif session.state == "ATTACHED":
                    # bootloader recovery path
                    snap = session.bridge.snapshot()
                    if not snap.get("running") or not snap.get("serial_alive") or not snap.get("vtty_alive"):
                        return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                    rx_tail_raw = session.bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)
                    rx_tail_clean = clean_text(rx_tail_raw)
                    matched = _matches_any_bootloader_prompt(rx_tail_clean, session.profile.bootloader_prompts)
                    # #114：末行匹配 bootloader prompt（如 => ）為既有路徑；若未匹配，
                    # 但 RX tail 命中 boot banner（U-Boot 版本行／autoboot 倒數行，複用
                    # #130 detect_boot_banner 單一事實來源），視為 autoboot 倒數窗，同樣
                    # 授予 recovery lease，並在回應標 boot_interrupt=True 供呼叫端連打按鍵
                    # 中斷 autoboot。
                    # #174：兩者皆未命中時，再檢查 rx tail 是否命中 login_regex／
                    # password_regex——board 完全健康、只差有人打帳密是「session 卡住但
                    # 板子正常」最常見的情境，語意上比 bootloader recovery 更安全（不必
                    # 整個 device release 出去）。命中則同樣授予 lease，回應標
                    # login_required=True，讓呼叫端用 interactive-send 打帳密救回 READY。
                    # 三者皆未命中才維持 NOT_BOOTLOADER。
                    boot_interrupt = False
                    login_required = False
                    if matched is None:
                        if detect_boot_banner(rx_tail_clean):
                            boot_interrupt = True
                        elif matches_login_or_password(rx_tail_clean, session.profile):
                            login_required = True
                        else:
                            return {
                                "ok": False,
                                "error_code": "SESSION_NOT_READY",
                                "selector": selector,
                                "error_detail": "NOT_BOOTLOADER",
                            }

                    if session.session_id in self._interactive_post_close:
                        return {
                            "ok": False,
                            "error_code": "SESSION_INTERACTIVE_BUSY",
                            "interactive_session_id": session.interactive_session_id,
                        }
                    # 清除 expired lease（避免 BUSY 誤判）；若 close 需要 resume，
                    # 先在 lock 外完成 post-close，再重新評估是否要 stash human。
                    existing, post = self._refresh_interactive_locked(session)
                    if post.needs_resume:
                        retry_after_post = True
                    else:
                        clamped_timeout = min(timeout_s, MAX_RECOVERY_LEASE_S)

                        if existing is None:
                            # 無現有 lease：直接開啟 recovery lease
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=clamped_timeout,
                                recovery_mode=True, suspended_human=False,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": True,
                            }

                        elif existing.owner.startswith("human:"):
                            # 有 human lease：stash and open recovery
                            human_id = existing.interactive_id
                            self._interactive.pop(human_id, None)
                            session.interactive_session_id = None
                            session._stashed_human_lease = existing
                            session.bridge.suspend_interactive()
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=clamped_timeout,
                                recovery_mode=True, suspended_human=True,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": True,
                            }

                        else:
                            # 有其他 agent lease：BUSY
                            return {
                                "ok": False,
                                "error_code": "SESSION_INTERACTIVE_BUSY",
                                "interactive_session_id": session.interactive_session_id,
                            }

                        # #114：banner 命中（autoboot 倒數窗）授予的 recovery lease，
                        # 於回應標 boot_interrupt=True；bootloader prompt 命中則省略此欄位
                        # （additive，與既有回應相容）。BUSY 路徑已提前 return，不會到此。
                        if boot_interrupt:
                            result["boot_interrupt"] = True
                        # #174：login/password prompt 命中授予的 recovery lease，於回應
                        # 標 login_required=True（比照 boot_interrupt 欄位模式，additive）。
                        if login_required:
                            result["login_required"] = True

                else:
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                if (
                    not retry_after_post
                    and command
                    and result.get("ok")
                    and session.bridge is not None
                ):
                    command_permit = TxPermit(
                        f"interactive:{lease.interactive_id}",
                        session.bridge_generation,
                        "interactive",
                        owner_gen=getattr(session.bridge, "owner_gen", 0),
                    )

            post.execute()
            if retry_after_post:
                continue
            break

        if command:
            assert command_permit is not None
            try:
                self._send_command_with_permit(
                    bridge,
                    command,
                    source=owner,
                    cmd_id=None,
                    permit=command_permit,
                )
            except TxIncompleteError as exc:
                reason = (exc.result.error or "").removeprefix("revoked:")
                if reason == "lease":
                    return {"ok": False, "error_code": "INTERACTIVE_EXPIRED"}
                raise
            self._mark_session_tx(session)
        return result

    def _encode_interactive_payload(self, data: str, encoding: str) -> bytes:
        if encoding == "plain":
            return data.encode("utf-8", errors="replace")
        if encoding == "base64":
            return base64.b64decode(data.encode("ascii"))
        key = data.strip().lower()
        key_map = {
            "enter": b"\n",
            "tab": b"\t",
            "escape": b"\x1b",
            "ctrl-c": b"\x03",
            "ctrl-d": b"\x04",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
        }
        if encoding == "key" and key in key_map:
            return key_map[key]
        raise ValueError("INVALID_INTERACTIVE_ENCODING")

    def interactive_send(self, interactive_id: str, *, data: str, encoding: str = "plain") -> dict[str, Any]:
        post = _PostCloseAction()
        result: dict[str, Any] | None = None
        send_capture: tuple[SessionRuntime, UARTBridge, InteractiveLease, bytes, TxPermit] | None = None
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.status == "expired":
                return {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            if lease.status != "active":
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.expired():
                session = self._sessions.get(lease.session_id)
                if session is not None:
                    _, post = self._close_interactive_locked(
                        session,
                        interactive_id=interactive_id,
                        terminal_status="expired",
                        keep_tombstone=True,
                    )
                else:
                    self._record_interactive_terminal_locked(
                        lease, terminal_status="expired", keep_tombstone=True,
                    )
                result = {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            else:
                session = self._sessions.get(lease.session_id)
                if session is None or session.bridge is None:
                    result = {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
                elif session.state == "FLASHING":
                    # FLASHING 期間禁止任何注入，避免汙染 SBL binary（C2）。
                    result = {"ok": False, "error_code": "FLASHING_BUSY", "interactive_id": interactive_id}
                elif session.foreground_busy or session._foreground_operation is not None:
                    result = {"ok": False, "error_code": "SESSION_BUSY", "interactive_id": interactive_id}
                else:
                    payload = self._encode_interactive_payload(data, encoding)
                    bridge = session.bridge
                    permit = TxPermit(
                        f"interactive:{interactive_id}",
                        session.bridge_generation,
                        "interactive",
                        owner_gen=getattr(bridge, "owner_gen", 0),
                    )
                    send_capture = (session, bridge, lease, payload, permit)
        post.execute()
        if send_capture is None:
            assert result is not None
            return result
        session, bridge, lease, payload, permit = send_capture
        try:
            self._send_bytes_with_permit(
                bridge,
                payload,
                source=lease.owner,
                cmd_id=None,
                permit=permit,
            )
        except TxIncompleteError as exc:
            reason = (exc.result.error or "").removeprefix("revoked:")
            if reason == "lease":
                return {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            if reason == "quiet":
                return {"ok": False, "error_code": ERROR_AUTOBOOT_QUIET, "interactive_id": interactive_id}
            if reason in {"draining", "cancelled", "stale_generation", "flashing"}:
                return {"ok": False, "error_code": "SESSION_BUSY", "interactive_id": interactive_id}
            raise
        self._mark_session_tx(session)
        with self._lock:
            current_lease = self._interactive.get(interactive_id)
            if current_lease is not None and current_lease.status == "active":
                current_lease.touch()
        return {"ok": True, "interactive_id": interactive_id, "bytes": len(payload)}

    def interactive_status(self, interactive_id: str, *, screen_chars: int = 2048) -> dict[str, Any]:
        post = _PostCloseAction()
        result: dict[str, Any]
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.status == "expired":
                return {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            if lease.status != "active":
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is None or session.bridge is None:
                result = {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
            elif lease.expired():
                _, post = self._close_interactive_locked(
                    session,
                    interactive_id=interactive_id,
                    terminal_status="expired",
                    keep_tombstone=True,
                )
                result = {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            else:
                # 先確認 session/bridge，再判斷期限；成功路徑仍以 touch 延長租約。
                lease.touch()
                result = {
                    "ok": True,
                    "interactive_id": interactive_id,
                    "owner": lease.owner,
                    "status": lease.status,
                    "recovery_mode": lease.recovery_mode,
                    "screen": clean_text(session.bridge.rx_tail(screen_chars)),
                    "session": session.to_public_dict(),
                }
        post.execute()
        return result

    def interactive_close(self, interactive_id: str) -> dict[str, Any]:
        post = _PostCloseAction()
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.status == "expired":
                return {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is not None:
                _, post = self._close_interactive_locked(session, interactive_id=interactive_id)
            else:
                self._interactive.pop(interactive_id, None)
        post.execute()
        return {"ok": True, "interactive_id": interactive_id}

    def self_test(self, selector: str, *, timeout_s: float = 2.0, strict_human_lock: bool = False) -> dict[str, Any]:
        """對外入口：在所有分支的最外層 result dict 注入 command_capable。

        #51 sub-task A：呼叫端不必鑽進巢狀 "session" dict 即可判斷該 session
        是否可下命令。SESSION_NOT_FOUND 等查無 session 的分支一律以 False 表示。
        """
        result = self._self_test_impl(
            selector, timeout_s=timeout_s, strict_human_lock=strict_human_lock
        )
        if "command_capable" not in result:
            # 直接取用 impl 在 lock 內快照進 result["session"] 的值，避免再開一次
            # lock 重撈 session（消除多餘鎖與 TOCTOU 窗口）。查無 session 的分支
            # （如 SESSION_NOT_FOUND）沒有 "session" key → 一律 False。
            nested = result.get("session")
            result["command_capable"] = (
                bool(nested.get("command_capable", False)) if nested is not None else False
            )
        return result

    def _self_test_impl(self, selector: str, *, timeout_s: float = 2.0, strict_human_lock: bool = False) -> dict[str, Any]:
        suspend_human_interactive = False
        post = _PostCloseAction()
        result: dict[str, Any] | None = None
        probe_permit: TxPermit | None = None
        # I2：RELEASED 分支須在 lock 內擷取、出 lock 後再掃 /proc（_probe_external_holder
        # 會掃整個 /proc，持 lock 期間會阻塞所有 RPC）。
        released_capture: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {
                    "ok": False,
                    "error_code": "SESSION_NOT_FOUND",
                    "selector": selector,
                    **self._lease_context(None),
                }
            if session.state == "RELEASED":
                device = self._devices.get(session.profile.device_by_id)
                released_capture = {
                    "real_path": device.real_path if device is not None else None,
                    "session": session.to_public_dict(),
                    "released_by": session.released_by,
                    "released_at": session.released_at,
                    "reason": session.released_reason,
                    "lease_context": self._lease_context(None),
                }
            if released_capture is None:
                lease = self._interactive.get(session.interactive_session_id) if session.interactive_session_id is not None else None
                if session.recovering:
                    return {
                        "ok": True,
                        "classification": "SESSION_RECOVERING",
                        "session": session.to_public_dict(),
                        "recommended_action": "wait",
                        **self._lease_context(lease, bridge=session.bridge),
                    }
                lease, post = self._refresh_interactive_locked(session)
                lease_context = self._lease_context(lease, bridge=session.bridge)
                device = self._devices.get(session.profile.device_by_id)
                attached_real_path = session.attached_real_path
                bridge = session.bridge
                if strict_human_lock and lease is not None and lease.owner.startswith("human:"):
                    result = {
                        "ok": True,
                        "classification": "HUMAN_INTERACTIVE_ACTIVE",
                        "interactive_id": lease.interactive_id,
                        "session": session.to_public_dict(),
                        "recommended_action": "wait_or_detach_console",
                        **lease_context,
                    }
                elif device is None:
                    result = {
                        "ok": True,
                        "classification": "DEVICE_MISSING",
                        "session": session.to_public_dict(),
                        "recommended_action": "check_cable_or_bind",
                        **lease_context,
                    }
                elif attached_real_path and attached_real_path != device.real_path:
                    result = {
                        "ok": True,
                        "classification": "DEVICE_REBOUND_REQUIRED",
                        "session": session.to_public_dict(),
                        "attached_real_path": attached_real_path,
                        "current_real_path": device.real_path,
                        "recommended_action": "reattach",
                        **lease_context,
                    }
                elif bridge is None:
                    result = {
                        "ok": True,
                        "classification": "BRIDGE_DOWN",
                        "session": session.to_public_dict(),
                        "current_real_path": device.real_path,
                        "recommended_action": "attach",
                        **lease_context,
                    }
                else:
                    snapshot = bridge.snapshot()
                    if not snapshot.get("running") or not snapshot.get("serial_alive"):
                        result = {
                            "ok": True,
                            "classification": "BRIDGE_DOWN",
                            "session": session.to_public_dict(),
                            "current_real_path": device.real_path,
                            "recommended_action": "recover",
                            **lease_context,
                        }
                    elif not snapshot.get("vtty_alive"):
                        result = {
                            "ok": True,
                            "classification": "VTTY_STALE",
                            "session": session.to_public_dict(),
                            "attached_vtty": snapshot.get("vtty"),
                            "recommended_action": "console_attach",
                            **lease_context,
                        }
                    elif (
                        session.profile.platform == "passthrough"
                        and session.state == "READY"
                    ):
                        result = {
                            "ok": True,
                            "classification": "PASSTHROUGH",
                            "session": session.to_public_dict(),
                            "attached_real_path": attached_real_path,
                            "current_real_path": device.real_path,
                            "attached_vtty": snapshot.get("vtty"),
                            "bridge_generation": session.bridge_generation,
                            "recommended_action": "none",
                            **lease_context,
                        }
                    elif session.state == "ATTACHED":
                        if session.profile.platform == "passthrough":
                            classification = "PASSTHROUGH"
                            recommended_action = "console_attach"
                            extra: dict[str, Any] = {
                                "passthrough_ready_in_s": self.passthrough_ready_in_s(
                                    session, time.monotonic()
                                )
                            }
                            # #181：fallback 是**未經量測**的 catch-all 分類，且空
                            # ready_probe 讓 command_capable 永久 false。把最近的 RX 拿去
                            # 比對所有 template，命中就直接給出可執行的出口，而不是只回
                            # console_attach——後者會讓使用者以為只能改設定檔＋重啟 daemon。
                            # 顯式宣告的 passthrough（yaml-target，如 uboot-template）是人為
                            # 決定，不在此勸退。
                            if session.profile_source == "fallback":
                                suggested = self._suggest_profile_from_rx(bridge)
                                if suggested is not None:
                                    com = session.profile.com
                                    recommended_action = "pin_profile"
                                    extra.update({
                                        "suggested_profile": suggested,
                                        "hint": (
                                            f"此 session 為未經量測的 fallback 分類，RX 內容匹配 "
                                            f"{suggested}。套用：serialwrap session pin --selector "
                                            f"{com} --profile {suggested} 之後 serialwrap session "
                                            f"clear --selector {com}。"
                                        ),
                                    })
                        elif session.last_error == "LOGIN_REQUIRED":
                            classification = "LOGIN_REQUIRED"
                            recommended_action = "console_attach"
                            extra = {}
                        elif session.last_error == "REBOOTING":
                            classification = "REBOOTING"
                            recommended_action = "wait_or_console_attach"
                            extra = {}
                        else:
                            # BOOTLOADER detection：讀取 RX tail 並比對 bootloader prompts。
                            # #162 C 組：樣式缺配置時退回 UBOOT_FALLBACK_PROMPTS（線上
                            # profile 漂移下仍認得）；reprobe 路徑已判定 BOOTLOADER_STUCK
                            # 時，即使此刻 tail 已被其他 RX 推走也維持分類（有理由的終態）。
                            rx_tail_raw = bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)
                            rx_tail_evidence = clean_text(rx_tail_raw)
                            matched = _matches_any_bootloader_prompt(
                                rx_tail_evidence,
                                session.profile.bootloader_prompts or UBOOT_FALLBACK_PROMPTS,
                            )
                            if matched is None and session.last_error == ERROR_BOOTLOADER_STUCK:
                                matched = (session.last_error_detail or "").removeprefix(
                                    "matched_prompt="
                                ) or ERROR_BOOTLOADER_STUCK
                            if matched is not None:
                                classification = "BOOTLOADER"
                                recommended_action = "recover_interactive"
                                extra = {"matched_prompt": matched, "rx_tail": rx_tail_evidence}
                            else:
                                # RX 洪水分類（#153）：last_error 已標 RX_FLOOD 或當下
                                # RX 速率仍超閾 → 明確告知「等排空」而非 ATTACHED_NOT_READY
                                # （後者會誤導上層去重建 session）。BOOTLOADER 判定優先。
                                flood_bytes = self._bridge_rx_bytes_last_10s(bridge)
                                if session.last_error == ERROR_RX_FLOOD or (
                                    flood_bytes is not None
                                    and flood_bytes >= RX_FLOOD_BYTES_PER_10S
                                ):
                                    classification = "RX_FLOOD"
                                    recommended_action = "wait"
                                    extra = {"rx_bytes_last_10s": flood_bytes}
                                else:
                                    classification = "ATTACHED_NOT_READY"
                                    recommended_action = "console_attach"
                                    extra = {}
                        result = {
                            "ok": True,
                            "classification": classification,
                            "session": session.to_public_dict(),
                            "attached_real_path": attached_real_path,
                            "current_real_path": device.real_path,
                            "attached_vtty": snapshot.get("vtty"),
                            "bridge_generation": session.bridge_generation,
                            "recommended_action": recommended_action,
                            **extra,
                            **lease_context,
                        }
                    elif session.boot_quiet_active():
                        # #130 review Finding 1b（必修）：session.state 仍是 READY，但
                        # RX 已見到 boot banner——因 boot quiet 只設 boot_quiet_until
                        # 不改 session.state，會出現「state=READY 但已在 quiet window」
                        # 的過渡態（#139 起 agent 顯式命令在此過渡態同樣被 AUTOBOOT_QUIET
                        # 拒絕，README §boot quiet window）。此時不送 nonce probe（等同
                        # 一般 probe，一樣會打斷 U-Boot autoboot），直接誠實回報仍在
                        # quiet window，等 RX 見 login/prompt 或逾時解除。
                        result = {
                            "ok": True,
                            "classification": ERROR_AUTOBOOT_QUIET,
                            "session": session.to_public_dict(),
                            "recommended_action": "wait",
                            **lease_context,
                        }
                    else:
                        nonce = uuid.uuid4().hex[:8]
                        probe = session.profile.ready_probe.replace("${nonce}", nonce)
                        probe_permit = self._self_test_permit_locked(session, nonce)
                        session.last_probe_at = now_iso()
                        prompt_regex = session.profile.prompt_regex
                        bridge_generation = session.bridge_generation
                        attached_vtty = snapshot.get("vtty")
                        current_real_path = device.real_path
                        suspend_human_interactive = lease is not None and lease.owner.startswith("human:")

        # I2：RELEASED 早退分支——出 lock 後才掃 /proc，避免阻塞所有 RPC。
        if released_capture is not None:
            real_path = released_capture["real_path"]
            holder = self._probe_external_holder(real_path) if real_path else {"pids": [], "holder": None}
            reclaimable = not holder["pids"]
            return {
                "ok": True,
                "classification": "RELEASED",
                "session": released_capture["session"],
                "released_by": released_capture["released_by"],
                "released_at": released_capture["released_at"],
                "reason": released_capture["reason"],
                "external_holder": holder["pids"] if holder["pids"] else "none",
                "reclaimable": reclaimable,
                "recommended_action": "device_attach" if reclaimable else "wait_external_flash",
                **released_capture["lease_context"],
            }

        post.execute()
        if result is not None:
            return result
        if suspend_human_interactive:
            bridge.suspend_interactive()
        try:
            offset = bridge.rx_snapshot_len()
            self._mark_session_tx(session)
            assert probe_permit is not None
            try:
                with self._system_permit_context(bridge, probe_permit):
                    bridge.send_command(probe, source="system:self_test", cmd_id=None)
            except TxIncompleteError as exc:
                if not _self_test_tx_refused(exc):
                    raise
                # 與 pre-check 的 AUTOBOOT_QUIET 回應保持相同形狀；提交點拒絕後不
                # 等 nonce，也不 confirm_ready，讓呼叫端沿用既有 wait 行動。
                with self._lock:
                    current_lease = (
                        self._interactive.get(session.interactive_session_id)
                        if session.interactive_session_id is not None
                        else None
                    )
                    return {
                        "ok": True,
                        "classification": ERROR_AUTOBOOT_QUIET,
                        "session": session.to_public_dict(),
                        "recommended_action": "wait",
                        **self._lease_context(current_lease, bridge=bridge),
                    }
            if not bridge.wait_for_regex_from(nonce, offset, timeout_s):
                # RX 洪水分類（#153）：nonce 被洪水淹沒 ≠ target 死了——RX 速率超閾時
                # 回 RX_FLOOD＋wait（而非 TARGET_UNRESPONSIVE＋recover），避免上層對
                # 活著的 console 做徒勞的 recover/重建。
                flood_bytes = self._bridge_rx_bytes_last_10s(bridge)
                if flood_bytes is not None and flood_bytes >= RX_FLOOD_BYTES_PER_10S:
                    return {
                        "ok": True,
                        "classification": "RX_FLOOD",
                        "session": session.to_public_dict(),
                        "attached_real_path": attached_real_path,
                        "current_real_path": current_real_path,
                        "probe_ok": False,
                        "rx_bytes_last_10s": flood_bytes,
                        "recommended_action": "wait",
                        **lease_context,
                    }
                return {
                    "ok": True,
                    "classification": "TARGET_UNRESPONSIVE",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": current_real_path,
                    "probe_ok": False,
                    "recommended_action": "recover",
                    **lease_context,
                }
            bridge.wait_for_regex_from(prompt_regex, offset, timeout_s)
            # #162：nonce 驗證成功＝READY 實證再確認，納入確認點（裁決 2，
            # 與各 READY 轉移點語意一致）。
            with self._lock:
                session.confirm_ready()
            return {
                "ok": True,
                "classification": "OK",
                "session": session.to_public_dict(),
                "attached_real_path": attached_real_path,
                "current_real_path": current_real_path,
                "attached_vtty": attached_vtty,
                "bridge_generation": bridge_generation,
                "probe_ok": True,
                "recommended_action": "none",
                **lease_context,
            }
        finally:
            if suspend_human_interactive:
                bridge.resume_interactive()

    def recover_session(self, selector: str, *, timeout_s: float = 2.0, force: bool = False) -> dict[str, Any]:
        reprobe = False
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session._pending_drain is not None:
                return {
                    "ok": False,
                    "error_code": "SESSION_RECOVERING",
                    "drain_stalled": session.drain_stalled,
                    "drain_timeout_epoch": session.drain_timeout_epoch,
                    "session": session.to_public_dict(),
                }
            # C2：RELEASED 早退——比照 clear_session，不改 state、不 spawn、不動集合。
            if session.state == "RELEASED" or session.profile.device_by_id in self._released_by_ids:
                return {
                    "ok": True,
                    "released": True,
                    "recommended_action": "device_attach",
                    "session": session.to_public_dict(),
                }
            # FLASHING 或 flash probe 臨界區，連 force recover 也不得介入——_force_recover 會 detach/重連
            # bridge、切斷進行中的燒錄/偵測 transport（#69 r5；#83 RACE-2 final：probe 窗口 state 仍非
            # FLASHING，但已標 flash-critical）。須先 exit_flashing 或待 probe 結束。
            if self._flash_busy_locked(session):
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com, "session": session.to_public_dict()}
            # 顯式人工 recover 視為重新介入：先清掉 reprobe 上限/進度，讓自動重探可在之後重新接手。
            # 否則 exhausted 的 ATTACHED session 一旦手動 recover 仍失敗，將永遠被 reconcile 跳過（Finding 4）。
            self._reset_reprobe_progress_locked(session)
            # 手動 recover 為重新介入：補帳密後允許重解析並再告警一次（#140）。
            session.credentials_warned = False
            if session.bridge is None:
                by_id = session.profile.device_by_id
                if by_id and by_id in self._devices:
                    session.state = "ATTACHING"
                    session.last_error = None
                    self._reset_reprobe_progress_locked(session)
                    self._spawn_attach(by_id)
                    return {"ok": True, "recovering": False, "action": "REATTACH", "session": session.to_public_dict()}
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            if session.state == "ATTACHED":
                if force:
                    return self._force_recover(session)
                bridge = session.bridge
                reprobe = True
            elif session.state != "READY":
                if force:
                    return self._force_recover(session)
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            else:
                bridge = session.bridge
                invalidate_deferred = getattr(bridge, "invalidate_deferred", None)
                if callable(invalidate_deferred):
                    invalidate_deferred("recover")
        if reprobe:
            # 與 auto 自動重探共用 per-session probe 互斥（Finding 2）。
            with self._lock:
                probe_lock = self._reprobe_probe_lock_locked(session.session_id)
            with probe_lock:
                result = self._probe_existing_bridge(session, bridge)
            if not result.get("ok"):
                if force:
                    return self._force_recover(session)
                return result
            recovered = result["session"]["state"] == "READY"
            payload: dict[str, Any] = {
                "ok": True,
                "recovering": False,
                "action": "REPROBE",
                "recovered": recovered,
                "session": result["session"],
            }
            if not recovered:
                payload["error_code"] = "SESSION_NOT_READY"
                payload["last_error"] = result["session"].get("last_error")
                payload["last_error_detail"] = result["session"].get("last_error_detail")
                # #162 C3：手動 recover 失敗且 RX tail 顯示板子卡在 bootloader → 回可
                # 行動分類，取代目前無資訊的 PROMPT_UNAVAILABLE（operator 才知道要開
                # interactive 手動 `boot`，而不是繼續無效重試）。
                matched = (
                    self._bootloader_evidence(session, bridge)
                    if session.profile.platform != "passthrough"
                    else None
                )
                if matched is not None or payload["last_error"] == ERROR_BOOTLOADER_STUCK:
                    payload["classification"] = "BOOTLOADER"
                    payload["recommended_action"] = "recover_interactive"
                    if matched is not None:
                        payload["matched_prompt"] = matched
                if force:
                    return self._force_recover(session)
            return payload
        return self._recover_after_failure(
            session,
            bridge,
            cmd_id="",
            timeout_s=timeout_s,
            source="system:recover",
            command="",
            prompt_regex=session.profile.prompt_regex,
            pre_offset=bridge.rx_snapshot_len(),
        )

    def _force_recover(self, session: SessionRuntime) -> dict[str, Any]:
        """Force recovery via clear + reattach + wait-ready."""
        selector = session.session_id
        self.clear_session(selector)
        import time
        for _ in range(10):
            time.sleep(1.0)
            state = self.get_session_state(selector)
            if state.get("ok") and state.get("session", {}).get("state") == "READY":
                return {
                    "ok": True,
                    "recovering": False,
                    "action": "FORCE_CLEAR_REATTACH",
                    "recovered": True,
                    "session": state["session"],
                }
        state = self.get_session_state(selector)
        return {
            "ok": False,
            "error_code": "FORCE_RECOVER_TIMEOUT",
            "action": "FORCE_CLEAR_REATTACH",
            "recovered": False,
            "session": state.get("session", {}),
        }

    def _evict_background_locked(self) -> None:
        """淘汰最舊的「已終結（非 active）」background capture，使數量回到上限（#81）。

        進行中的 capture 永不淘汰；全為 active 時暫時超量（待其完成後下次淘汰）。
        須在持有 ``self._lock`` 下呼叫。
        """
        if len(self._background) <= BG_CAPTURE_MAX_COUNT:
            return
        finalized = [(cid, cap) for cid, cap in self._background.items() if cap.status != "active"]
        finalized.sort(key=lambda kv: kv[1].created_at)
        excess = len(self._background) - BG_CAPTURE_MAX_COUNT
        for cid, cap in finalized[:excess]:
            self._background.pop(cid, None)
            # 同步從擁有者 session 的 background_cmd_ids 移除：否則該 per-session list 只在 detach 時
            # clear，會隨長壽 daemon 無界成長，且 _on_bridge_rx 每筆 RX 都會逐一掃描它（記憶體 + per-RX
            # CPU 累積），即使 _background 已被 BG_CAPTURE_MAX_COUNT 封頂亦然（#81 Codex 必修）。
            owner = self._sessions.get(cap.session_id)
            if owner is not None:
                try:
                    owner.background_cmd_ids.remove(cid)
                except ValueError:
                    pass

    def get_background_result(self, cmd_id: str, *, from_chunk: int = 0, limit: int = 200) -> dict[str, Any]:
        with self._lock:
            capture = self._background.get(cmd_id)
            if capture is None:
                return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
            capture.maybe_finalize()
            # 環形上限下最舊 dropped_chunks 段已丟棄；以絕對 chunk 索引對齊 list 位置（#81）。
            # dropped_chunks==0（未丟棄，常態）時行為與原本完全相同。
            start = max(from_chunk - capture.dropped_chunks, 0)
            # chunks 為 deque（O(1) popleft），不支援切片；以 islice 取窗口（讀路徑非熱點）。
            chunks = list(itertools.islice(capture.chunks, start, start + limit))
            returned_from = capture.dropped_chunks + start  # 實際回傳第一段的絕對索引
            next_chunk = returned_from + len(chunks)
            return {
                "ok": True,
                "cmd_id": cmd_id,
                "status": capture.status,
                "error_code": capture.error_code,
                "from_seq": capture.from_seq,
                "last_seq": capture.last_seq,
                "from_chunk": from_chunk,
                "next_chunk": next_chunk,
                "dropped_chunks": capture.dropped_chunks,
                "lost": from_chunk < capture.dropped_chunks,
                "chunks": chunks,
            }

    # ------------------------------------------------------------------
    # Agent log capture: start / stop / status
    # ------------------------------------------------------------------

    def _resolve_log_dir(self, session: SessionRuntime) -> str:
        return session.profile.log_dir or LOG_DIR

    def _stop_capture_locked(self, session: SessionRuntime) -> None:
        cap = session.active_capture
        if cap is None:
            return
        cap.status = "stopped"
        fp = self._capture_fps.pop(cap.capture_id, None)
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        session.active_capture = None

    def log_start(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.active_capture is not None and session.active_capture.status == "active":
                cap = session.active_capture
                return {
                    "ok": True,
                    "already_active": True,
                    "capture_id": cap.capture_id,
                    "log_path": cap.log_path,
                    "session": session.to_public_dict(),
                }
            log_dir = self._resolve_log_dir(session)
            os.makedirs(log_dir, exist_ok=True)
            ts = time.strftime("%y%m%d-%H%M%S")
            filename = f"{session.profile.com}_{ts}.log"
            log_path = os.path.join(log_dir, filename)
            capture_id = str(uuid.uuid4())
            try:
                fp = open(log_path, "a", encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error_code": "LOG_OPEN_FAILED", "detail": str(exc)}
            cap = SessionCapture(
                capture_id=capture_id,
                session_id=session.session_id,
                log_path=log_path,
                started_at=now_iso(),
            )
            session.active_capture = cap
            self._capture_fps[capture_id] = fp
            return {
                "ok": True,
                "capture_id": capture_id,
                "log_path": log_path,
                "session": session.to_public_dict(),
            }

    def log_stop(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            cap = session.active_capture
            if cap is None:
                return {"ok": False, "error_code": "NO_ACTIVE_CAPTURE", "session": session.to_public_dict()}
            result = {
                "ok": True,
                "capture_id": cap.capture_id,
                "log_path": cap.log_path,
                "line_count": cap.line_count,
                "byte_count": cap.byte_count,
                "started_at": cap.started_at,
            }
            self._stop_capture_locked(session)
            return result

    def log_status(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            cap = session.active_capture
            if cap is None:
                return {"ok": True, "active": False, "session": session.to_public_dict()}
            return {
                "ok": True,
                "active": cap.status == "active",
                "capture_id": cap.capture_id,
                "log_path": cap.log_path,
                "line_count": cap.line_count,
                "byte_count": cap.byte_count,
                "started_at": cap.started_at,
                "session": session.to_public_dict(),
            }

    # ── 檔案傳輸 ──────────────────────────────────────────────

    def _file_tx_rejected_result(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        permit: TxPermit,
        exc: TxIncompleteError,
    ) -> dict[str, Any]:
        """把檔案傳輸中被拒的 TX 映射為 file.* 錯誤碼（S1X-a-4、OD-62）。

        - ``quiet`` 且被拒的這筆零寫出 → ``AUTOBOOT_QUIET``＋``retry_after_s``（SC-03）。
        - drain、世代過期、取消（OD-62，比照 BC-03）：整段傳輸已確認寫出 ≥1 byte →
          ``COMMAND_ABORTED_IN_FLIGHT``，並比照一般命令寫 command_aborted_in_flight WAL
          META（cmd_id 為傳輸的 cmd_id 前綴）；零寫出 → ``FLUSHED_BY_RECOVERY``。``quiet``
          但該筆已部分寫出者同屬已寫出。
        - 其餘（結果不明、flashing 等）→ 傳輸自身的 ``TRANSFER_TIMEOUT``（0.3.2 結束碼）。

        push 另附 ``chunks_sent``／``chunks_total``。
        """
        progress = {
            key: getattr(exc, key)
            for key in ("chunks_sent", "chunks_total")
            if getattr(exc, key, None) is not None
        }
        reason = (exc.result.error or "").removeprefix("revoked:")
        if reason == "quiet" and exc.result.confirmed == 0:
            if session.boot_quiet_active():
                retry_after_s = round(
                    max(session.boot_quiet_until - time.monotonic(), 0.0), 1
                )
            else:
                retry_after_s = READY_RECONFIRM_RETRY_S
            return {
                "ok": False,
                "error_code": ERROR_AUTOBOOT_QUIET,
                "selector": session.profile.com,
                "retry_after_s": retry_after_s,
                **progress,
            }
        if reason in {"quiet", "cancelled", "stale_generation", "draining"}:
            written = getattr(exc, "operation_confirmed", None)
            if written is None:
                written = exc.result.confirmed
            if written > 0:
                self._append_aborted_in_flight_meta(
                    bridge,
                    getattr(exc, "transfer_cmd_id", None) or permit.op_id,
                    written,
                    exc.result,
                    unknown=isinstance(exc, TxUnknownError) or exc.result.outcome == "unknown",
                )
                return {"ok": False, "error_code": "COMMAND_ABORTED_IN_FLIGHT", **progress}
            return {"ok": False, "error_code": "FLUSHED_BY_RECOVERY", **progress}
        return {"ok": False, "error_code": "TRANSFER_TIMEOUT", **progress}

    def file_push(
        self,
        selector: str,
        *,
        local_path: str,
        remote_path: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_timeout_s: float | None = None,
        ack_mode: str = "auto",
        echo_timeout_s: float | None = None,
        source: str = "agent",
        trusted_human: bool | None = None,
    ) -> dict[str, Any]:
        """將 host 端檔案推送到 target。

        ``chunk_timeout_s``（#157）：單一 chunk 等待 target 回 prompt 的逾時；
        ``None`` 時沿用 ``profile.timeout_s``（夾 ``_MIN_FILE_CHUNK_TIMEOUT_S`` 地板），
        取代舊版寫死的 10.0s——bcm 類慢板已調大的 ``timeout_s`` 因此自動生效。

        ``ack_mode``（#161）：chunk 命令行送出方式（``auto``/``echo``/``none``），
        見 :func:`sw_core.file_transfer.push_file`；slice 大小維持模組固定保守值
        （plan 決策 2，不開放自適應）。

        ``echo_timeout_s``（#161 實機調校）：單一 slice 等待 echo 回讀的逾時；
        ``None`` 時比照 ``chunk_timeout_s`` 的推導精神沿用 ``profile.timeout_s``
        （夾 ``_MIN_FILE_ECHO_TIMEOUT_S``＝5.0 地板）——bcm 類已調大 ``timeout_s``
        的慢板因此自動放寬。調高只影響失敗路徑的等待上限，成功路徑吞吐不變。
        """
        from .file_transfer import push_file

        human = source.startswith("human:") if trusted_human is None else trusted_human
        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        operation: _ForegroundOperation | None = None
        transfer_permit: TxPermit | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.profile.platform == "passthrough":
                return passthrough_file_transfer_error(session.to_public_dict())
            if session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            # #139：同 execute_command 的第二層 gate——file 傳輸直寫 bridge、不經
            # execute_command，須各自補 gate（僅開始前擋；傳輸中 banner 到達不中途
            # abort，由傳輸自然 timeout，屬既有行為）。
            # 直接 manager 呼叫沿用既有 source 語意；RPC 路徑明確傳入可信來源旗標。
            if not human:
                gate = self._agent_gate_reject_locked(session)
                if gate is not None:
                    return gate
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None:
                if not human and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
            profile_timeout_s = session.profile.timeout_s
            max_console_line_chars = session.profile.max_console_line_chars
            if busy_result is None:
                operation, busy_result = self._begin_foreground_operation_locked(
                    session,
                    kind="file",
                )
                transfer_permit = TxPermit(
                    f"file:{uuid.uuid4().hex}",
                    session.bridge_generation,
                    "human_line" if human else "agent",
                )

        human_suspended = False
        try:
            post.execute()
            if busy_result is not None:
                return busy_result
            effective_timeout_s = (
                chunk_timeout_s if chunk_timeout_s is not None
                else max(profile_timeout_s, _MIN_FILE_CHUNK_TIMEOUT_S)
            )
            effective_echo_timeout_s = (
                echo_timeout_s if echo_timeout_s is not None
                else max(profile_timeout_s, _MIN_FILE_ECHO_TIMEOUT_S)
            )
            if suspend_human_interactive:
                bridge.suspend_interactive()
                human_suspended = True
            assert transfer_permit is not None
            try:
                with self._operation_permit_context(bridge, transfer_permit):
                    return push_file(
                        bridge,
                        local_path,
                        remote_path,
                        chunk_size=chunk_size,
                        timeout_s=effective_timeout_s,
                        prompt_regex=prompt_regex,
                        source=source,
                        ack_mode=ack_mode,
                        echo_timeout_s=effective_echo_timeout_s,
                        max_console_line_chars=max_console_line_chars,
                    )
            except TxIncompleteError as exc:
                return self._file_tx_rejected_result(session, bridge, transfer_permit, exc)
        finally:
            try:
                if human_suspended:
                    bridge.resume_interactive()
            finally:
                self._finish_foreground_operation(session, operation)

    def file_pull(
        self,
        selector: str,
        *,
        remote_path: str,
        local_path: str | None = None,
        chunk_timeout_s: float | None = None,
        source: str = "agent",
        trusted_human: bool | None = None,
    ) -> dict[str, Any]:
        """從 target 拉取檔案到 host。

        ``chunk_timeout_s``（#157）：整段 base64 讀取的逾時；``None`` 時沿用
        ``profile.timeout_s``（夾 ``_MIN_FILE_PULL_TIMEOUT_S`` 地板，維持舊版
        30.0s 行為基準），取代舊版寫死的 30.0s。
        """
        from .file_transfer import pull_file

        human = source.startswith("human:") if trusted_human is None else trusted_human
        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        operation: _ForegroundOperation | None = None
        transfer_permit: TxPermit | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.profile.platform == "passthrough":
                return passthrough_file_transfer_error(session.to_public_dict())
            if session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            # #139：同 file_push 的 gate（pull 同樣直寫 bridge、不經 execute_command）。
            # 直接 manager 呼叫沿用既有 source 語意；RPC 路徑明確傳入可信來源旗標。
            if not human:
                gate = self._agent_gate_reject_locked(session)
                if gate is not None:
                    return gate
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None:
                if not human and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
            profile_timeout_s = session.profile.timeout_s
            max_console_line_chars = session.profile.max_console_line_chars
            if busy_result is None:
                operation, busy_result = self._begin_foreground_operation_locked(
                    session,
                    kind="file",
                )
                transfer_permit = TxPermit(
                    f"file:{uuid.uuid4().hex}",
                    session.bridge_generation,
                    "human_line" if human else "agent",
                )

        human_suspended = False
        try:
            post.execute()
            if busy_result is not None:
                return busy_result
            effective_timeout_s = (
                chunk_timeout_s if chunk_timeout_s is not None
                else max(profile_timeout_s, _MIN_FILE_PULL_TIMEOUT_S)
            )
            if suspend_human_interactive:
                bridge.suspend_interactive()
                human_suspended = True
            assert transfer_permit is not None
            try:
                with self._operation_permit_context(bridge, transfer_permit):
                    return pull_file(
                        bridge,
                        remote_path,
                        local_path,
                        timeout_s=effective_timeout_s,
                        prompt_regex=prompt_regex,
                        source=source,
                        max_console_line_chars=max_console_line_chars,
                    )
            except TxIncompleteError as exc:
                return self._file_tx_rejected_result(session, bridge, transfer_permit, exc)
        finally:
            try:
                if human_suspended:
                    bridge.resume_interactive()
            finally:
                self._finish_foreground_operation(session, operation)


def _test_kill_after_save_hook(release_kill_point: bool) -> None:
    """Stage 0 實機測試鉤子（T21）：``SessionManager._save_state`` 落盤成功後、回應送達前，模擬程序被殺。

    只有 release 持久化（``release_kill_point=True``，即 ``_persist_release_state``）才可能終止行程；
    建構子與其他 ``_save_state`` 呼叫不終止，否則 daemon 在收到任何 RPC 前就會死。
    僅隔離實例生效（SC-19 判定：STATE／RUN／CONFIG 三目錄皆明確設定且非預設值）；
    非隔離實例設定 ``SERIALWRAP_TEST_KILL_AFTER_SAVE=1`` 時只寫 WARNING、不終止行程。
    置於模組尾端並延遲 import，使既有 logger 呼叫的行號（``contracts/daemon_logging.yaml``）不位移。
    """
    if (os.environ.get("SERIALWRAP_TEST_KILL_AFTER_SAVE") or "").strip() != "1":
        return
    from .constants import default_isolation_paths
    from .test_allowlist import isolation_reason

    reason = isolation_reason(os.environ, default_isolation_paths())
    if reason is not None:
        logging.getLogger("serialwrap").warning(
            "SERIALWRAP_TEST_KILL_AFTER_SAVE=1 已忽略：%s（非隔離實例不得終止行程）", reason)
        return
    if not release_kill_point:
        return
    logging.getLogger("serialwrap").warning(
        "SERIALWRAP_TEST_KILL_AFTER_SAVE=1：state 已落盤，於回應前以 os._exit(137) 模擬程序被殺（隔離實例）")
    os._exit(137)


def _test_release_persist_point() -> bool:
    """Stage 0 實機測試鉤子（T21）：release 持久化的 commit 前暫停點；回傳 True 表示本次為 release 持久化。

    呼叫端 ``_persist_release_state`` 經模組層 ``_save_release_state`` 先取得旗標、再呼叫 ``_save_state()``，
    使暫停點位於 state.json 落盤前、且不增加 session_manager 既有行號（keep-baseline 以行號釘住部分 logger 來源）。
    僅隔離實例生效：環境變數 ``SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE`` 指向目錄時，行程寫入 ``arrived`` 標記後，
    每 50 ms 輪詢 ``go`` 標記（上限 600 s）才繼續；操作者看到 ``arrived`` 後記錄 state.json 快照並 kill -9，
    可確定 kill 發生在 commit 之前。非隔離實例只寫 WARNING、不暫停。
    """
    raw = (os.environ.get("SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE") or "").strip()
    if not raw:
        return True
    from .constants import default_isolation_paths
    from .test_allowlist import isolation_reason

    reason = isolation_reason(os.environ, default_isolation_paths())
    if reason is not None:
        logging.getLogger("serialwrap").warning(
            "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE 已忽略：%s（非隔離實例不得暫停）", reason)
        return True
    os.makedirs(raw, exist_ok=True)
    with open(os.path.join(raw, "arrived"), "w", encoding="utf-8") as fp:
        fp.write(f"pid={os.getpid()}\n")
    go_path = os.path.join(raw, "go")
    deadline = time.monotonic() + 600.0
    while not os.path.exists(go_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return True


def _save_release_state(manager: "SessionManager") -> None:
    """release 持久化（``_persist_release_state``）專用的落盤入口：組合 T21 兩個隔離實例測試鉤子。

    順序固定為「commit 前暫停點 → ``manager._save_state()`` → 落盤後 kill 鉤子（release 旗標為 True）」。
    ``_save_state`` 維持無參數簽章，使既有以無參數替身 monkeypatch ``_save_state`` 的測試
    （T08 ``test_release_persist_failure_keeps_blocking`` 等）仍走真實 release 持久化路徑；
    ``_save_state`` 內部的鉤子呼叫一律帶 False（建構子與其他存檔不得終止行程）。
    置於模組尾端，使既有 logger 呼叫的行號（``contracts/daemon_logging.yaml``）不位移。
    """
    release_kill_point = _test_release_persist_point()
    manager._save_state()
    _test_kill_after_save_hook(release_kill_point)


_READLINE_WRAP_MIN_SEGMENT = 20


def _match_readline_wrapped_echo(text: str, command: str) -> tuple[int, int] | None:
    """回傳首個非空行內「命令原文＋readline 折行標記」的起訖位置。"""
    if not text or not command:
        return None

    line_start = 0
    while line_start < len(text):
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if line.strip("\r"):
            break
        if line_end == len(text):
            return None
        line_start = line_end + 1
    else:
        return None

    for command_start in range(len(line)):
        if line[command_start] != command[0]:
            continue
        cursor = command_start
        matched = 0
        last_wrap: int | None = None
        while matched < len(command) and cursor < len(line):
            if line.startswith(" \r", cursor) and matched > 0:
                segment = matched if last_wrap is None else matched - last_wrap
                if segment < _READLINE_WRAP_MIN_SEGMENT:
                    break
                last_wrap = matched
                cursor += 2
                continue
            if line[cursor] == command[matched]:
                cursor += 1
                matched += 1
                continue
            break
        if matched != len(command):
            continue
        if cursor == len(line) or line[cursor:] == "\r":
            return line_start + command_start, line_start + cursor
    return None
