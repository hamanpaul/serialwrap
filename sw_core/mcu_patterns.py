"""MCU flash pattern registry — 定義各 MCU 家族的非破壞性 probe/ACK 位元組與鮑率。

每筆 pattern 必須通過 non_destructive 審核，才能載入 registry。
"""
from __future__ import annotations
import dataclasses


def _hex_to_bytes(s: str) -> bytes:
    """將十六進位字串（支援空白與 0x 前綴）轉換為 bytes。"""
    return bytes.fromhex(s.replace(" ", "").replace("0x", ""))


@dataclasses.dataclass(frozen=True)
class McuPattern:
    """單一 MCU 家族的 flash 識別 pattern。

    Attributes:
        family: MCU 家族識別名稱，例如 ``"ti-cc26xx"``。
        probe: 傳送至 MCU 的非破壞性 sync 位元組。
        expect: 期望收到的 ACK 位元組。
        baud: 通訊鮑率。
        timeout_ms: 等待 ACK 的逾時（毫秒）。
        non_destructive: 必須為 True；False 會在 from_dict 被拒絕。
    """

    family: str
    probe: bytes        # 非破壞性 sync 位元組
    expect: bytes       # 期望的 ACK 位元組
    baud: int
    timeout_ms: int
    non_destructive: bool = True

    def __post_init__(self) -> None:
        # 守衛收斂於此：無論經 from_dict 或直接建構，未通過非破壞審核一律拒絕，
        # 杜絕日後（含測試）繞過 from_dict 直接塞入破壞性 pattern 的缺口。
        if not self.non_destructive:
            raise ValueError(
                f"pattern {self.family!r} 未通過 non_destructive 審核，拒絕載入"
            )

    @classmethod
    def from_dict(cls, d: dict) -> "McuPattern":
        """從字典建立 McuPattern；若 non_destructive 為 False（或未提供）則拋出 ValueError。"""
        return cls(
            family=str(d["family"]),
            probe=_hex_to_bytes(str(d["probe"])),
            expect=_hex_to_bytes(str(d["expect"])),
            baud=int(d.get("baud", 115200)),
            timeout_ms=int(d.get("timeout_ms", 500)),
            non_destructive=bool(d.get("non_destructive", False)),
        )


# 內建預設 pattern 清單（僅含已審核的非破壞性 pattern）
_DEFAULTS: list[dict] = [
    {
        "family": "ti-cc26xx",
        "probe": "5555",          # TI CC26xx 的 UART bootloader sync 位元組
        "expect": "00cc",         # ACK（0x00）+ 固定識別碼（0xCC）
        "baud": 115200,
        "timeout_ms": 500,
        "non_destructive": True,
    },
]


class McuPatternRegistry:
    """管理所有已知 MCU 家族的 flash pattern。"""

    def __init__(self, patterns: list[McuPattern]) -> None:
        # 以 family 名稱為 key，方便快速查詢
        self._by_family: dict[str, McuPattern] = {p.family: p for p in patterns}

    @classmethod
    def default(cls) -> "McuPatternRegistry":
        """建立僅含內建預設 pattern 的 registry。"""
        return cls([McuPattern.from_dict(d) for d in _DEFAULTS])

    @classmethod
    def load(cls, rows: list[dict] | None) -> "McuPatternRegistry":
        """建立含預設 + 自訂 pattern 的 registry；自訂 pattern 同樣須通過 non_destructive 審核。"""
        patterns = [McuPattern.from_dict(d) for d in _DEFAULTS]
        for d in rows or []:
            patterns.append(McuPattern.from_dict(d))  # non_destructive guard 在 from_dict
        return cls(patterns)

    def get(self, family: str) -> McuPattern:
        """以家族名稱取得 McuPattern；找不到時拋出 KeyError。"""
        return self._by_family[family]

    def all(self) -> list[McuPattern]:
        """回傳所有已登錄的 McuPattern 清單。"""
        return list(self._by_family.values())

    def render_support_list(self, *, candidates: list[dict]) -> str:
        """產生人類可讀的支援家族清單與候選 COM port 列表字串。

        Args:
            candidates: 尚未判斷家族的 COM port 描述列表，每筆含 ``com``、
                        ``by_id``、``real_path`` 欄位。
        Returns:
            格式化後的多行字串，供 CLI 或 log 輸出使用。
        """
        lines = ["serialwrap MCU flash — 支援家族："]
        for p in self.all():
            lines.append(
                f"  - {p.family}: probe={p.probe.hex(' ')} expect={p.expect.hex(' ')} "
                f"baud={p.baud}"
            )
        lines.append("")
        lines.append("目前候選（已排除 command_capable console）：")
        if not candidates:
            lines.append("  （無）")
        for c in candidates:
            lines.append(f"  - {c.get('com')} {c.get('by_id')} -> {c.get('real_path')}")
        return "\n".join(lines) + "\n"
