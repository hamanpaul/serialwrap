"""CLI stdout／stderr 輸出契約測試（P3-T11）。

驗證 JSON 輸出格式、key 排序、stderr 錯誤行、版本警告順序與 BrokenPipe 退出碼。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bb_harness import NL, cli_prefix, repo_root, run_cli

pytestmark = [pytest.mark.process_rpc]


@pytest.mark.core_portable
def test_rpc_fail_order_separated(env, stubd):
    """一般 RPC 失敗時：stderr 第 1 行為錯誤訊息，第 2 行為版本警告，stdout 為排序後的 JSON。"""
    cli_version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    s = stubd({
        "methods": {
            "session.clear": [
                {
                    "reply": {
                        "ok": False,
                        "error_code": "SESSION_NOT_FOUND",
                        "message": "m",
                        "version": "9.9.9",
                    }
                }
            ]
        }
    })
    r = run_cli(["--socket", s.endpoint, "session", "clear", "--selector", "A"], env=env)
    assert r.rc == 2
    assert (
        r.stdout
        == b'{"error_code":"SESSION_NOT_FOUND","message":"m","ok":false,"version":"9.9.9"}' + NL
    )
    err_lines = r.stderr.decode("utf-8").splitlines()
    assert len(err_lines) == 2
    assert err_lines[0] == "serialwrap: session.clear failed: SESSION_NOT_FOUND: m"
    assert err_lines[1] == (
        f"serialwrap: client 版本 {cli_version} 與 daemon 版本 9.9.9 不一致"
        "（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）"
    )


@pytest.mark.core_portable
def test_event_fail_order(env, stubd):
    """event 子命令失敗時：stderr 第 1 行為版本警告，第 2 行為錯誤訊息（與一般 RPC 順序相反）。"""
    cli_version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    s = stubd({
        "methods": {
            "event.rule_delete": [
                {
                    "reply": {
                        "ok": False,
                        "error_code": "SESSION_NOT_FOUND",
                        "message": "m",
                        "version": "9.9.9",
                    }
                }
            ]
        }
    })
    r = run_cli(["--socket", s.endpoint, "event", "rm", "r1"], env=env)
    assert r.rc == 2
    err_lines = r.stderr.decode("utf-8").splitlines()
    assert len(err_lines) == 2
    assert err_lines[0] == (
        f"serialwrap: client 版本 {cli_version} 與 daemon 版本 9.9.9 不一致"
        "（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）"
    )
    assert err_lines[1] == "serialwrap: event.rule_delete failed: SESSION_NOT_FOUND: m"


@pytest.mark.core_portable
def test_merged_stream_order_matches_python(env, stubd):
    """合流（merge_stderr=True）時：stderr 兩行在前、stdout JSON 在後（Q-06）。"""
    cli_version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    s = stubd({
        "methods": {
            "session.clear": [
                {
                    "reply": {
                        "ok": False,
                        "error_code": "SESSION_NOT_FOUND",
                        "message": "m",
                        "version": "9.9.9",
                    }
                }
            ]
        }
    })
    r = run_cli(
        ["--socket", s.endpoint, "session", "clear", "--selector", "A"],
        env=env,
        merge_stderr=True,
    )
    assert r.rc == 2
    assert r.stderr == b""
    expected = (
        f"serialwrap: session.clear failed: SESSION_NOT_FOUND: m\n"
        f"serialwrap: client 版本 {cli_version} 與 daemon 版本 9.9.9 不一致"
        "（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n"
        '{"error_code":"SESSION_NOT_FOUND","message":"m","ok":false,"version":"9.9.9"}\n'
    ).encode("utf-8")
    if os.name == "nt":
        expected = expected.replace(b"\n", b"\r\n")
    assert r.stdout == expected


@pytest.mark.core_portable
def test_ok_no_stderr(env, stubd):
    """成功回應且版本一致時：stderr 恆為空。"""
    cli_version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    s = stubd({"methods": {"session.clear": [{"reply": {"ok": True, "version": cli_version}}]}})
    r = run_cli(["--socket", s.endpoint, "session", "clear", "--selector", "A"], env=env)
    assert r.rc == 0
    assert r.stderr == b""


@pytest.mark.core_portable
def test_broken_pipe_large_write_exit_1(env):
    """stdout 讀端已關閉時，大輸出（>= 8192 B）在寫入當下即失敗，回傳 exit 1（Q-20）。"""
    cmd = cli_prefix() + ["skill"]
    r_fd, w_fd = os.pipe()
    try:
        os.close(r_fd)
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=w_fd,
            stderr=subprocess.PIPE,
        )
        os.close(w_fd)
        rc = proc.wait(timeout=30)
    finally:
        pass
    assert rc == 1


@pytest.mark.core_portable
def test_broken_pipe_small_output_exit_120(env, stubd):
    """stdout 讀端已關閉時，小輸出（< 8192 B）在行程結束 flush 時才失敗，回傳 exit 120（Q-20）。"""
    s = stubd({"methods": {"wal.current_seq": [{"reply": {"ok": True, "seq": 1}}]}})
    cmd = cli_prefix() + ["--socket", s.endpoint, "wal", "current-seq"]
    r_fd, w_fd = os.pipe()
    try:
        os.close(r_fd)
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=w_fd,
            stderr=subprocess.PIPE,
        )
        os.close(w_fd)
        rc = proc.wait(timeout=30)
    finally:
        pass
    assert rc == 120
