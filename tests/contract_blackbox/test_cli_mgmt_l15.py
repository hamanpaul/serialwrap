"""Stage 1B 管理面 L1.5 整合與不變量測試（P4-T16）。

L1.5 必須以 P2 bridge＋DUT 模擬器（posix_pty）驗證 §0.4 不變量：
- 被拒請求外部 TX 為零（zero_tx_on_reject）
- 不同來源 bytes 不得交錯（no_forbidden_interleave）
- 切換不得回放失效輸入（no_stale_context_replay）
- 新 daemon 開 tty 須在舊 daemon fd 關閉之後（no_reopen_before_handoff）

本檔目前**不具備**可執行的 L1.5 基礎設施，因此每個案例以 `BLOCKED-PREREQ` 明確失敗，不以 xfail 隱藏，
也不保留無法證明不變量的假斷言。阻塞條件與各案例的通過判準如下（併入 feature/rust-refactor 後補實作）：

1. test_update_busy_l15_zero_tx（SC-23）：需 P2 bridge 與 DUT 模擬器；以真實 `setup` 旗標製造 SESSION_BUSY／FLASHING_BUSY，
   斷言錯誤碼正確且 wire TX delta == 0。
2. test_update_file_push_after_last_probe_l15（SC-23）：需產品端 `SW_TEST_UPDATE_PAUSE_AFTER` 測試鉤子；
   並行 1 MiB `file push` 於最後探測之後，斷言 SESSION_BUSY／FLASHING_BUSY，或 FLUSHED_BY_SHUTDOWN 加 reason=handoff_frozen，且 TX delta == 0。
3. test_update_push_after_freeze_zero_tx_l15（R1-F1，SC-23）：需先製造 freeze（上列鉤子）；push 被拒時 TX delta == 0。
4. test_reject_conflict_l15_zero_tx（BC-05）：先起外部 daemon 佔住 tty；斷言 DAEMON_ALREADY_RUNNING_ELSEWHERE 且 DUT TX == 0。
5. test_restart_no_stale_console_replay（SC-20）：先把 marker 寫入 human console 緩衝並做正向對照（未重啟時 marker 出現於 RX），
   再 restart；斷言新 daemon 後 DUT RX 不含該 marker。
6. test_restart_no_reopen_before_handoff（SC-20）：以 holder pid 配對 bridge control.ndjson 的 opened／closed 事件，
   斷言舊 holder 的 closed 早於新 holder 的 opened（不得以 max(closed) < min(opened) 比較，因舊 daemon 自己的 opened 必早於其 closed）。

test_autostart_l15_tx_once 已自本檔移除：POSIX 無法以 caps 覆寫 env 模擬 SC-18 的 autostart 路徑，
其 TX 斷言不可用 `session list`（零 UART TX）驗證；對應案例 sc18 的承接節點為 test_cli_mgmt_daemon.py::test_autostart_l15_tx_once（Windows 原生）。
"""
from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.posix_pty,
]

BLOCKED_L15 = (
    "BLOCKED-PREREQ（L1.5）：需 P2 bridge＋DUT 模擬器（feature/rust-refactor 的 func-test/runner.py --mode bridge 與 "
    "scripts/docker/run-l15.sh），且 setup --target-version 等真實旗標與產品端測試鉤子尚未在本分支具備；"
    "本案例尚未實作，不得視為通過。"
)


def _blocked(detail: str) -> None:
    pytest.fail(f"{BLOCKED_L15} 細節：{detail}")


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_busy_l15_zero_tx(tmp_path) -> None:
    """SC-23：SESSION_BUSY／FLASHING_BUSY 拒絕更新時 DUT TX 為 0。"""
    _blocked("待實作：真實 setup 旗標製造忙碌狀態，斷言錯誤碼與 wire TX delta == 0")


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_file_push_after_last_probe_l15(tmp_path) -> None:
    """SC-23：最後探測之後並行 1 MiB file push 必須被阻斷，且不得在 handoff 後送出 TX。"""
    _blocked("待實作：SW_TEST_UPDATE_PAUSE_AFTER 鉤子＋並行 1 MiB file push；斷言 SESSION_BUSY／FLASHING_BUSY 或 FLUSHED_BY_SHUTDOWN+reason=handoff_frozen，TX delta == 0")


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_push_after_freeze_zero_tx_l15(tmp_path) -> None:
    """R1-F1：凍結後 push 必須被拒且 DUT TX 為 0。"""
    _blocked("待實作：先製造 freeze（鉤子），push 被拒時 wire TX delta == 0")


@pytest.mark.case("bc05-process-lifecycle-stop-restart")
def test_reject_conflict_l15_zero_tx(tmp_path) -> None:
    """DAEMON_ALREADY_RUNNING_ELSEWHERE 拒絕時 DUT TX 為 0（外部 daemon 先佔住 tty）。"""
    _blocked("待實作：先起外部 daemon 佔住 tty，斷言錯誤碼與 DUT TX == 0")


@pytest.mark.case("sc20-daemon-restart")
def test_restart_no_stale_console_replay(tmp_path) -> None:
    """restart 前 human console 緩衝未送出之行，新 daemon 起來後不得出現在 DUT RX（含正向對照）。"""
    _blocked("待實作：marker 寫入 console 緩衝→正向對照（未重啟時 RX 出現）→restart→斷言 RX 不含 marker")


@pytest.mark.case("sc20-daemon-restart")
def test_restart_no_reopen_before_handoff(tmp_path) -> None:
    """新 daemon 開 tty 之前，舊 daemon 的 fd 必須已關閉（以 holder pid 配對 opened／closed）。"""
    _blocked("待實作：control.ndjson 以 holder pid 配對，斷言 old.closed < new.opened")
