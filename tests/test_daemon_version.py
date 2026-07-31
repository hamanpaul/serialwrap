"""#154：daemon 端版本可診斷性——`SerialwrapService` 常駐版本、`health()`／`rpc()` 皆帶
`version` 欄位。

daemon 原本沒有任何等價於 CLI `_resolve_version()` 的解析器；`health.status`
（`daemon status`）回應完全不帶版本欄位，逼得 preflight 得繞 `/proc cmdline +
importlib.metadata`。本檔涵蓋：

- `SerialwrapService.__init__` 後 `svc._version == resolve_version()`。
- `svc.health().get("version")` 與 `svc.rpc("health.status", {}).get("version")`
  皆等於 `resolve_version()`。
- 非 `health.status` 的既有方法（`health.ping`／`mcu.patterns`）回應也帶
  `version`——證明是 `rpc()`→`_dispatch()` wrapper 在補欄位，不是逐一 handler 手動加。
- 未知 method（`_dispatch()` 尾端 catch-all 回 `METHOD_NOT_FOUND`）不因新 wrapper
  而拋例外，且同樣拿到 version 欄位。
"""
from __future__ import annotations

from unittest import mock

from sw_core.service import SerialwrapService
from sw_core.version import resolve_version


def _make_service() -> SerialwrapService:
    return SerialwrapService([])


_FAKE_MULTI_OPEN = {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "ok"}


def test_service_caches_version_at_init() -> None:
    svc = _make_service()
    assert svc._version == resolve_version()


def test_health_includes_version() -> None:
    svc = _make_service()
    with mock.patch("sw_core.service.detect_multi_open", return_value=_FAKE_MULTI_OPEN):
        st = svc.health()
    assert st.get("version") == resolve_version()


def test_rpc_health_status_includes_version() -> None:
    svc = _make_service()
    with mock.patch("sw_core.service.detect_multi_open", return_value=_FAKE_MULTI_OPEN):
        st = svc.rpc("health.status", {})
    assert st.get("ok") is True
    assert st.get("version") == resolve_version()


def test_rpc_wrapper_adds_version_to_non_health_methods() -> None:
    """非 health.status 的既有方法（health.ping／mcu.patterns）也帶 version——
    證明版本欄位由 rpc() wrapper 的 setdefault 統一補上，非逐一 handler 手動加。"""
    svc = _make_service()
    ping = svc.rpc("health.ping", {})
    assert ping == {"ok": True, "pong": True, "version": resolve_version()}
    patterns = svc.rpc("mcu.patterns", {})
    assert patterns.get("ok") is True
    assert patterns.get("version") == resolve_version()


def test_rpc_unknown_method_does_not_raise_and_still_gets_version() -> None:
    """未知 method 現行落到 `_dispatch()` 尾端的 catch-all
    `{"ok": False, "error_code": "METHOD_NOT_FOUND", ...}`（非隱式 None——design
    doc 對此的描述有誤，已依實測修正本測試）；新 wrapper 對此仍是 dict 的情況
    照常補上 version，不拋例外。"""
    svc = _make_service()
    result = svc.rpc("no.such.method", {})
    assert result["ok"] is False
    assert result["error_code"] == "METHOD_NOT_FOUND"
    assert result.get("version") == resolve_version()


def test_dispatch_directly_has_no_version_injected() -> None:
    """`_dispatch()`（機械 rename 前的 `rpc()` 本體）本身不做版本注入——
    版本欄位是 `rpc()` wrapper 這一層加的，鎖住兩者職責分離。"""
    svc = _make_service()
    raw = svc._dispatch("health.ping", {})
    assert raw == {"ok": True, "pong": True}
    assert "version" not in raw
