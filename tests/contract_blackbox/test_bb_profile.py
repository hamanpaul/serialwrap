"""黑箱契約測試：profile test 指令（P3-T15）。

驗證 profile 離線正則測試、未知 profile 報錯、遺失樣本報錯、預設目錄解析、
YAML 語法錯誤 exit 1 與 CRLF 樣本讀取。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bb_harness import isolated_env, run_cli

pytestmark = [pytest.mark.process_rpc]

DATA_PROFILES_DIR = Path(__file__).parent / "data" / "profiles"


@pytest.mark.core_portable
def test_success(env):
    sample = str(DATA_PROFILES_DIR / "s.txt")
    pdir = str(DATA_PROFILES_DIR)
    r = run_cli(
        ["profile", "test", "--profile", "op3-template", "--sample", sample, "--profile-dir", pdir],
        env=env,
    )
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is True
    assert data["profile"] == "op3-template"
    assert data["platform"] == "shell"
    assert data["profile_dir"] == pdir
    assert data["sample"] == sample
    assert data["checks"]["bootloader_prompts"][0]["matched"] is True
    assert data["checks"]["bootloader_prompts"][0]["matched_line"] == "=> "


@pytest.mark.core_portable
def test_unknown_profile_available_sorted(env):
    sample = str(DATA_PROFILES_DIR / "s.txt")
    pdir = str(DATA_PROFILES_DIR)
    r = run_cli(
        ["profile", "test", "--profile", "nope", "--sample", sample, "--profile-dir", pdir],
        env=env,
    )
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "UNKNOWN_PROFILE"
    assert data["profile"] == "nope"
    assert data["profile_dir"] == pdir
    # Q-26：available 只含最後一個 YAML 檔（b.yaml）的 template
    assert data["available"] == ["op3-template"]


@pytest.mark.core_portable
def test_sample_missing(env):
    pdir = str(DATA_PROFILES_DIR)
    r = run_cli(
        ["profile", "test", "--profile", "op3-template", "--sample", "nope.txt", "--profile-dir", pdir],
        env=env,
    )
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data == {
        "error_code": "SAMPLE_READ_FAILED",
        "message": "[Errno 2] No such file or directory: 'nope.txt'",
        "ok": False,
    }


@pytest.mark.core_portable
def test_default_profile_dir_from_env(tmp_path):
    cfg_dir = tmp_path / "config"
    pdir = cfg_dir / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATA_PROFILES_DIR / "b.yaml", pdir / "b.yaml")
    sample = tmp_path / "s.txt"
    shutil.copy(DATA_PROFILES_DIR / "s.txt", sample)

    e = isolated_env(tmp_path, SERIALWRAP_CONFIG_DIR=str(cfg_dir))
    r = run_cli(
        ["profile", "test", "--profile", "op3-template", "--sample", str(sample)],
        env=e,
    )
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is True
    assert data["profile_dir"] == str(pdir)


@pytest.mark.core_portable
def test_yaml_syntax_error_exit_1(tmp_path, env):
    bad_dir = tmp_path / "bad_profiles"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "bad.yaml").write_text("bad: [unclosed yaml", encoding="utf-8")
    sample = tmp_path / "sample.txt"
    sample.write_text("hello\n", encoding="utf-8")

    r = run_cli(
        ["profile", "test", "--profile", "op3-template", "--sample", str(sample), "--profile-dir", str(bad_dir)],
        env=env,
    )
    assert r.rc == 1
    assert r.stdout == b""


@pytest.mark.core_portable
def test_crlf_sample(tmp_path, env):
    pdir = str(DATA_PROFILES_DIR)
    crlf_sample = tmp_path / "crlf_sample.txt"
    crlf_sample.write_bytes(b"host login: \r\nPassword: \r\nroot@host:~# \r\n=> \r\n")

    r = run_cli(
        ["profile", "test", "--profile", "op3-template", "--sample", str(crlf_sample), "--profile-dir", pdir],
        env=env,
    )
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is True
    assert data["checks"]["login_regex"]["matched"] is True
