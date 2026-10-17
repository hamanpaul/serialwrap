"""OD-11／SC-19：隔離實例專用的測試裝置 allowlist 判定（Stage 0 Python 基線）。

規則（凍結，見 docs/rust-refactor/plans/02-stage0-python-bc.md Task T13）：

- 只對隔離實例生效：``SERIALWRAP_STATE_DIR``、``SERIALWRAP_RUN_DIR``、``SERIALWRAP_CONFIG_DIR``
  三者都明確設定且各自解析後不等於預設路徑；任一不符即拒絕啟動。
- 只接受明確列出的 COM 名稱（``DeviceInfo.by_id`` 精確比對），不得全域放行。
- 只影響裝置發現與選取；不關閉獨占鎖、不更換 serial backend。
- 判定失敗以 ``RuntimeError(ERROR_TEST_DEVICE_ALLOWLIST_REJECTED, reason)`` 表達，由
  ``sw_core.daemon`` 擷取後寫 stderr 並以 exit 2 結束（不取 lock、不建立任何狀態檔）。
- ``config.yaml`` 存在但無法讀取或解析：隔離實例 fail closed（allowlist 可能藏在其中，不得退回全域掃描）；
  非隔離實例維持「未設定」，與既有 daemon 對壞 config 的容錯一致（見 :func:`decide_on_unreadable_config`）。
"""
from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from sw_core.device_watcher import DeviceInfo

if TYPE_CHECKING:
    from sw_core.device_source import DeviceSource

ERROR_TEST_DEVICE_ALLOWLIST_REJECTED = "TEST_DEVICE_ALLOWLIST_REJECTED"
ENV_TEST_DEVICE_ALLOWLIST = "SERIALWRAP_TEST_DEVICE_ALLOWLIST"
ISOLATION_ENV_VARS: tuple[str, ...] = (
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_CONFIG_DIR",
)
INVALID_CONFIG_REASON = "invalid windows.test_device_allowlist"


@dataclasses.dataclass(frozen=True)
class AllowlistDecision:
    """allowlist 判定結果。

    ``enabled``：allowlist 已通過判定並生效。被拒絕（``rejected_reason`` 非 None）時恆為 False，
    ``coms`` 為空；daemon 必須拒絕啟動，此物件不會傳入 ``SerialwrapService``。
    """

    enabled: bool
    coms: tuple[str, ...]
    source: Literal["env", "config", "none"]
    rejected_reason: str | None


def _dedup(items: Iterable[str]) -> tuple[str, ...]:
    """去除空項後保留原始順序、去重（先出現者保留）。"""
    return tuple(dict.fromkeys(item for item in items if item))


def _normalize_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _config_allowlist(config_data: Any) -> tuple[str, ...] | None:
    """讀取 ``windows.test_device_allowlist``；未設定回 None，型別錯誤拋 ValueError。"""
    if not isinstance(config_data, Mapping):
        return None
    windows = config_data.get("windows")
    if not isinstance(windows, Mapping) or "test_device_allowlist" not in windows:
        return None
    value = windows["test_device_allowlist"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(INVALID_CONFIG_REASON)
    return _dedup(item.strip() for item in value)


def _isolation_reason(env: Mapping[str, str], defaults: Mapping[str, str]) -> str | None:
    """依 STATE、RUN、CONFIG 順序回報第一個不符隔離條件的項目；全部符合回 None。"""
    for var in ISOLATION_ENV_VARS:
        raw = (env.get(var) or "").strip()
        if not raw:
            return f"not an isolated instance: {var} unset"
        default = defaults.get(var)
        if default is None:
            # 缺少比對基準時無法證明「非預設」，一律視為非隔離（fail closed，不得默默略過判定）。
            return f"not an isolated instance: {var} default unknown"
        if _normalize_path(raw) == _normalize_path(default):
            return f"not an isolated instance: {var} equals default"
    return None


def resolve_test_allowlist(
    env: Mapping[str, str],
    config_data: Mapping[str, Any] | None,
    *,
    defaults: Mapping[str, str],
) -> AllowlistDecision:
    """依 env（優先）與 config 決定測試裝置 allowlist 是否生效。

    env 的 ``SERIALWRAP_TEST_DEVICE_ALLOWLIST`` 有非空白內容即採用 env（即使解析後為空，亦不回退 config）；
    否則採用 ``windows.test_device_allowlist``。兩者皆無或解析後為空 → ``enabled=False``、``source="none"``。
    列表非空時才做隔離判定；被拒時回傳 ``rejected_reason``。
    """
    raw_env = env.get(ENV_TEST_DEVICE_ALLOWLIST)
    if raw_env is not None and raw_env.strip():
        source: Literal["env", "config"] = "env"
        coms = _dedup(item.strip() for item in raw_env.split(","))
    else:
        source = "config"
        try:
            configured = _config_allowlist(config_data)
        except ValueError as exc:
            return AllowlistDecision(False, (), "config", str(exc))
        if configured is None:
            return AllowlistDecision(False, (), "none", None)
        coms = configured

    if not coms:
        return AllowlistDecision(False, (), "none", None)

    reason = _isolation_reason(env, defaults)
    if reason is not None:
        return AllowlistDecision(False, (), source, reason)
    return AllowlistDecision(True, coms, source, None)


def decide_on_unreadable_config(
    env: Mapping[str, str],
    *,
    defaults: Mapping[str, str],
    error: str,
) -> AllowlistDecision:
    """``config.yaml`` 存在但無法讀取或解析時的判定；``error`` 為例外類別名（單行、不含內容）。

    - env 已提供非空 allowlist：config 不被採用，依 env 正常判定（與 :func:`resolve_test_allowlist` 相同）。
    - 非隔離實例：無法得知 config 是否含 allowlist，維持未設定（``source="none"``），與既有 daemon
      對壞 config 的容錯一致，正式實例啟動行為不變。
    - 隔離實例：allowlist 可能藏在無法解析的 config 中；fail closed 拒絕啟動
      （``invalid config.yaml: <error>``），避免測試實例默默退回全域裝置掃描。
    """
    raw_env = env.get(ENV_TEST_DEVICE_ALLOWLIST)
    if raw_env is not None and raw_env.strip():
        return resolve_test_allowlist(env, None, defaults=defaults)
    if _isolation_reason(env, defaults) is not None:
        return AllowlistDecision(False, (), "none", None)
    return AllowlistDecision(False, (), "config", f"invalid config.yaml: {error}")


def enforce_allowlist(decision: AllowlistDecision) -> None:
    """判定被拒時拋出 ``RuntimeError(碼, 原因)``；通過則不做任何事。

    注意：碼必須以字面字串傳入 ``RuntimeError``——``contracts/code_extraction.yaml`` 的
    ``call_arg RuntimeError`` sink 帶有 ``value_pattern``，只擷取字串常數、不解析名稱引用；
    字面值須與 ``ERROR_TEST_DEVICE_ALLOWLIST_REJECTED`` 相同。
    """
    if decision.rejected_reason is not None:
        raise RuntimeError("TEST_DEVICE_ALLOWLIST_REJECTED", decision.rejected_reason)


def filter_allowlist(devices: Mapping[str, DeviceInfo], coms: tuple[str, ...]) -> dict[str, DeviceInfo]:
    """只保留 ``DeviceInfo.by_id`` 精確列於 ``coms`` 的裝置（保留原 key）。"""
    allowed = frozenset(coms)
    return {key: info for key, info in devices.items() if info.by_id in allowed}


class AllowlistDeviceSource:
    """包住既有 ``DeviceSource``，掃描結果經 ``filter_allowlist`` 過濾（只影響發現與選取）。"""

    def __init__(self, inner: DeviceSource, coms: tuple[str, ...]) -> None:
        self._inner = inner
        self._coms = coms

    def scan(self) -> dict[str, DeviceInfo]:
        return filter_allowlist(self._inner.scan(), self._coms)
