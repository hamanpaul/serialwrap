"""#174 — `serialwrap profile test` CLI 子命令：離線 regex 診斷。

不連 daemon、不碰任何 UART：從 --profile-dir 載入 profile YAML，對 --sample
檔案文字跑 prompt/login/password/bootloader regex，輸出 JSON 回報命中結果，
exit code 恆為 0（純診斷）。
"""
from __future__ import annotations

import json

import sw_core.cli as cli


_PROFILE_YAML = """
profiles:
  brcm-template:
    platform: bcm
    prompt_regex: "(?m)^(?:.*[^>#\\\\s])?[>#][ \\\\t]*$"
    login_regex: "(?mi)login:\\\\s*$"
    password_regex: "(?mi)password:\\\\s*$"
    post_login_cmd: "sh"
    bootloader_prompts:
      - "^CFE> $"
      - "^=> $"
"""


def _write_profile_dir(tmp_path):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "default.yaml").write_text(_PROFILE_YAML, encoding="utf-8")
    return profile_dir


def test_profile_test_reports_prompt_match(tmp_path, capsys):
    profile_dir = _write_profile_dir(tmp_path)
    sample = tmp_path / "sample.txt"
    sample.write_text("root@host:~# ", encoding="utf-8")

    rc = cli.main([
        "profile", "test",
        "--profile", "brcm-template",
        "--sample", str(sample),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile"] == "brcm-template"
    assert payload["checks"]["prompt_regex"]["matched"] is True
    assert payload["checks"]["prompt_regex"]["matched_line"] == "root@host:~# "


def test_profile_test_matched_line_is_full_line_on_midline_match(tmp_path, capsys):
    """review：regex 命中行內片段時，matched_line 仍須回完整原始行（供目視核對），
    不得回 sample_text[:m.end()] 的截斷片段。"""
    profile_dir = _write_profile_dir(tmp_path)
    sample = tmp_path / "sample.txt"
    # login_regex "(?mi)login:\s*$" 的命中點在行中段（"login:" 之後的 \s*$ 不含
    # 行首前綴）——matched_line 必須回含前綴的完整原始行，而非 m.end() 截到的片段
    sample.write_text("noise before\nsome-hostname login: \nnoise after\n", encoding="utf-8")

    rc = cli.main([
        "profile", "test",
        "--profile", "brcm-template",
        "--sample", str(sample),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["login_regex"]["matched"] is True
    assert payload["checks"]["login_regex"]["matched_line"] == "some-hostname login: "


def test_profile_test_reports_prompt_no_match_on_banner(tmp_path, capsys):
    profile_dir = _write_profile_dir(tmp_path)
    sample = tmp_path / "sample.txt"
    sample.write_text("#########################################\n(none) login: ", encoding="utf-8")

    rc = cli.main([
        "profile", "test",
        "--profile", "brcm-template",
        "--sample", str(sample),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["prompt_regex"]["matched"] is False
    assert payload["checks"]["login_regex"]["matched"] is True
    assert payload["checks"]["login_regex"]["matched_line"] == "(none) login: "


def test_profile_test_reports_bootloader_prompts_list(tmp_path, capsys):
    profile_dir = _write_profile_dir(tmp_path)
    sample = tmp_path / "sample.txt"
    sample.write_text("U-Boot 2021.01\n=> ", encoding="utf-8")

    rc = cli.main([
        "profile", "test",
        "--profile", "brcm-template",
        "--sample", str(sample),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    bl = payload["checks"]["bootloader_prompts"]
    assert len(bl) == 2
    matched_patterns = {c["pattern"] for c in bl if c["matched"]}
    assert matched_patterns == {"^=> $"}


def test_profile_test_unknown_profile_returns_ok_false(tmp_path, capsys):
    profile_dir = _write_profile_dir(tmp_path)
    sample = tmp_path / "sample.txt"
    sample.write_text("whatever", encoding="utf-8")

    rc = cli.main([
        "profile", "test",
        "--profile", "does-not-exist",
        "--sample", str(sample),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0  # 純診斷，不因命中失敗而非零 exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "UNKNOWN_PROFILE"
    assert "brcm-template" in payload["available"]


def test_profile_test_missing_sample_file_returns_ok_false(tmp_path, capsys):
    profile_dir = _write_profile_dir(tmp_path)

    rc = cli.main([
        "profile", "test",
        "--profile", "brcm-template",
        "--sample", str(tmp_path / "does-not-exist.txt"),
        "--profile-dir", str(profile_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "SAMPLE_READ_FAILED"
