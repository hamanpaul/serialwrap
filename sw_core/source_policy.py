"""`--source`／interactive `--owner` 來源字串的共用驗證與分類。

背景：命令的「human／agent」身分、以及內部強制恢復旗標，全靠來源字串**前綴**判斷：

- ``source.startswith("human:")``：human 來源——不受 boot quiet gate、命令逾時不送
  CTRL-C／CTRL-D 而是升級成 interactive lease（``_recover_after_failure``、
  ``_resolve_session_id``、``execute_command``、``file_push``／``file_pull``）。
- ``source == "system:recover"``：``_recover_after_failure`` 逾時後額外跑
  ``_force_recover``（clear + reattach）。內部 ``recover_session`` 以這個來源呼叫。
- ``system``／``system:*``：daemon 內部送 UART 位元組時的 WAL 歸因標籤
  （``system:recover``／``system:self_test``／``system:secret``／``system``）。

外部入口（CLI／RPC）的字串若打錯（``--source human`` 少冒號）會被默默當 agent，
逾時時多送 CTRL-C／CTRL-D 把 console 登出；若冒用 ``system:recover`` 則可觸發
force clear + reattach。本模組在**外部入口**統一處理這兩類，不動內部呼叫：

- ``human`` 或 ``human:``（沒名字，不分大小寫）：**正規化**為 ``human:cli``。呼叫端
  意圖明確（就是 human），拒絕只會逼所有 human 腳本改參數；正規化後回應帶
  ``source_normalized_from``，不是默默改。
- ``system``／``system:*``（不分大小寫）：**拒絕**，``SOURCE_RESERVED``。外部沒有任何
  正當理由用內部保留標籤，放行只會污染 WAL 歸因並可能觸發內部旗標。
- 其餘（``agent:<名>``、``agent``、``cli``、``codex``…）：原樣放行，維持 agent 行為。

只有外部入口呼叫本模組；內部直接呼叫 ``arbiter.submit``／``bridge.send_*`` 的路徑
（human console 行 ``human:<client_id>``、``recover_session``、``self-test``、login
probe）不經此驗證。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ERROR_SOURCE_RESERVED = "SOURCE_RESERVED"

SOURCE_CLASS_HUMAN = "human"
SOURCE_CLASS_AGENT = "agent"

HUMAN_PREFIX = "human"
DEFAULT_HUMAN_NAME = "cli"
# 內部保留的來源前綴（冒號前的 token，不分大小寫）；外部入口一律拒絕。
RESERVED_PREFIXES: tuple[str, ...] = ("system",)


@dataclass(frozen=True)
class SourceCheck:
    """``check_source`` 的結果：``error`` 非 None 代表拒絕（可直接當 RPC 回應）。"""

    source: str
    normalized_from: str | None = None
    error: dict[str, Any] | None = None


def classify_source(source: str) -> str:
    """與內部判斷一致：``human:`` 開頭為 human，其餘（含未知前綴）為 agent。"""
    return SOURCE_CLASS_HUMAN if source.startswith(f"{HUMAN_PREFIX}:") else SOURCE_CLASS_AGENT


def check_source(
    raw: object,
    *,
    default: str,
    field: str = "source",
    normalize_human: bool = True,
) -> SourceCheck:
    """驗證並正規化外部傳入的來源字串。

    ``raw`` 為空／全空白時回 ``default``（沿用各入口原本的 ``or "agent"``／``or "cli"``）。
    ``normalize_human=False`` 時不處理 ``human``（interactive ``--owner`` 用：正規化成
    ``human:cli`` 會開出沒有 console peer 的 human lease，反而比原本的 agent lease 糟）。
    ``field`` 只影響錯誤回應的欄位名（``source``／``owner``）。
    """
    text = str(raw) if raw else ""
    if not text.strip():
        return SourceCheck(source=default)
    head, _, tail = text.partition(":")
    token = head.strip().casefold()
    if token in RESERVED_PREFIXES:
        return SourceCheck(
            source=text,
            error={
                "ok": False,
                "error_code": ERROR_SOURCE_RESERVED,
                "field": field,
                "value": text,
                "reserved_prefix": token,
                "hint": (
                    f"`{token}` 與 `{token}:*` 是 daemon 內部保留的來源前綴（WAL 歸因與內部恢復旗標），"
                    "外部呼叫不可使用。自動化請用 `agent:<名>`，人工操作請用 `human:<名>`；"
                    "逾時行為差異見 skill「`--source` 的語意」。"
                ),
            },
        )
    if token == HUMAN_PREFIX and normalize_human:
        canonical = f"{HUMAN_PREFIX}:{tail.strip() or DEFAULT_HUMAN_NAME}"
        return SourceCheck(source=canonical, normalized_from=text if canonical != text else None)
    return SourceCheck(source=text)
