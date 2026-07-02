"""#120 向量 2：--socket 明確性判準改 sentinel（有傳即明確），杜絕等值誤判路由到 live daemon。"""
from __future__ import annotations

import argparse

import sw_core.cli as cli


def _ns(endpoint=None, socket=None):
    return argparse.Namespace(endpoint=endpoint, socket=socket)


def test_socket_equal_to_default_is_explicit(monkeypatch):
    """傳入恰等於預設 SOCKET_PATH 的 --socket 須被尊重：直接回傳、不讀 config。"""
    def _boom():
        raise AssertionError("不得 fallback 讀 config（--socket 已明確指定）")

    monkeypatch.setattr(cli, "_safe_runtime_config", _boom)
    assert cli._resolve_endpoint(_ns(socket=cli.SOCKET_PATH)) == cli.SOCKET_PATH


def test_empty_socket_is_explicit(monkeypatch):
    """空字串也屬明確傳入的 --socket；不得再 fallback 到 config/default。"""
    def _boom():
        raise AssertionError("不得 fallback 讀 config（--socket 已明確指定）")

    monkeypatch.setattr(cli, "_safe_runtime_config", _boom)
    assert cli._resolve_endpoint(_ns(socket="")) == ""


def test_no_socket_falls_back_to_config(monkeypatch):
    class _RC:
        def socket_path(self):
            return "/cfg/live.sock"

        def mode(self):
            return "on-demand"

    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: _RC())
    monkeypatch.setattr(cli, "_endpoint_alive", lambda p: True)
    assert cli._resolve_endpoint(_ns()) == "/cfg/live.sock"


def test_no_socket_no_config_uses_default(monkeypatch):
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: None)
    assert cli._resolve_endpoint(_ns()) == cli.SOCKET_PATH


def test_explicit_endpoint_still_wins(monkeypatch):
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: None)
    assert cli._resolve_endpoint(_ns(endpoint="tcp://127.0.0.1:1", socket="/x")) == "tcp://127.0.0.1:1"


def test_parser_socket_default_is_none():
    """argparse default 必須是 None sentinel。"""
    parser = cli.build_parser()
    args = parser.parse_args(["session", "list"])
    assert args.socket is None
