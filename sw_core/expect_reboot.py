"""``cmd submit --expect-reboot`` 的共用驗證與提示：所有提交／執行入口在排隊／TX 前共用。"""

from __future__ import annotations

from typing import Any

ERROR_INVALID_EXPECT_REBOOT = "INVALID_EXPECT_REBOOT"
ERROR_EXPECT_REBOOT_MODE = "EXPECT_REBOOT_MODE_UNSUPPORTED"
WARNING_CHAINED_REBOOT = "CHAINED_REBOOT_WARNING"


def valid_expect_reboot(value: object) -> bool:
    """None／bool 皆合法；其他型別（字串 ``"false"``、整數）一律拒絕，不做真值轉換。"""
    return value is None or isinstance(value, bool)


def expect_reboot_rejection(value: object, mode: object) -> dict[str, Any] | None:
    """回傳拒絕回應（零 TX）或 ``None``（放行）。

    ``mode=interactive`` 是 lease 按鍵通道，不經 reboot 路徑，旗標在該模式下沒有任何
    效果；與其默默忽略讓呼叫端誤以為已受保護，不如直接拒絕。
    """
    if not valid_expect_reboot(value):
        return {"ok": False, "error_code": ERROR_INVALID_EXPECT_REBOOT}
    if value is True and mode == "interactive":
        return {
            "ok": False,
            "error_code": ERROR_EXPECT_REBOOT_MODE,
            "hint": "--expect-reboot 只適用 line／background 模式；interactive 是 lease 按鍵通道，不走 reboot 路徑。",
        }
    return None


def chained_reboot_warning(segment: str) -> dict[str, Any]:
    """沒帶 ``--expect-reboot`` 但串接命令含 reboot 區段時的非致命提示。"""
    return {
        "code": WARNING_CHAINED_REBOOT,
        "segment": segment,
        "hint": (
            "命令在 &&／||／;／& 之後含 reboot，但未帶 --expect-reboot：broker 只認單獨一條的 reboot，"
            "這條會被當一般命令，不會立即 arm 開機靜默窗；shell 不回 prompt 的板子會逾時並送 "
            "Ctrl-C／Ctrl-D，shell 立刻重印 prompt 的板子則後續命令可能落進正在 shutdown 的系統。"
            "含重開機的串接命令請改用 `cmd submit --expect-reboot`。命令仍照常送出，本提示不改變執行。"
        ),
    }


def add_submit_warning(result: dict[str, Any], warning: dict[str, Any]) -> None:
    """把非致命提示併進 submit 回應，既有 ``warning`` 單一欄位的形狀維持不變。

    ``warning`` 維持第一則（既有 ``CMD_LENGTH_WARNING`` 優先）；``warnings`` 列出全部，
    僅在有新提示時才出現，故沒有新提示的回應與舊版逐位元組相同。
    """
    existing = result.get("warning")
    result["warnings"] = ([existing] if isinstance(existing, dict) else []) + [warning]
    result.setdefault("warning", warning)
