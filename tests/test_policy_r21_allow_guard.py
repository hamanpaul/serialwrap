"""R-21 整檔豁免的防線（雲端審查 d2118cd_be2c4c6 6ad67f1#1 方案 (b)）。

`.project-policy.yml` 的 `secret_scan.allow` 依 policy engine `_is_exempt` 為**整檔豁免**：
放行個人路徑樣式的同時，也停用 credential 與 marker 偵測器。逐字複製的雲端 oracle 審查原文
只為第 3 行的雲端容器工作樹路徑而列入 allow，故本測試以 engine 同一組正規式掃描該檔，要求
credential 與 marker 命中為 0、structural 命中恰為第 3 行一處；日後編輯若夾帶憑證或新路徑即 FAIL。

engine（`policy_check`）不在 CI 的 pytest 依賴內：可匯入時直接取用其正規式並核對本檔釘選副本
未漂移；不可匯入時改用釘選副本（仍執行，不 SKIP）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
GUARDED = "docs/rust-refactor/reviews/s1x-oracle-review-1.md"
EXPECTED_STRUCTURAL_LINES = [3]

# policy engine v1.0.19（2dcf2f8）`policy_check/rules/r21_secret_scan.py` 的正規式釘選副本。
_PINNED = {
    "_STRUCTURAL": (r"/home/[a-z_][a-z0-9_-]*/", re.IGNORECASE),
    "_PRIVATE_KEY": (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
    "_AWS_ACCESS_KEY": (r"\b(?:AKIA|ASIA|A3T|AIDA|AGPA)[A-Z0-9]{16}\b", 0),
    "_GITHUB_TOKEN": (r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", 0),
}


def _engine():
    try:
        from policy_check.rules import r21_secret_scan
    except ImportError:
        return None
    return r21_secret_scan


def _config() -> dict:
    return yaml.safe_load((ROOT / ".project-policy.yml").read_text(encoding="utf-8"))


def _patterns() -> dict[str, re.Pattern[str]]:
    engine = _engine()
    if engine is None:
        return {name: re.compile(src, flags) for name, (src, flags) in _PINNED.items()}
    return {name: getattr(engine, name) for name in _PINNED}


def _marker_re() -> re.Pattern[str] | None:
    engine = _engine()
    if engine is None:
        tokens = {str(t).lower() for t in ((_config().get("secret_scan") or {}).get("markers") or [])}
        if not tokens:
            return None
        return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(tokens)) + r")\b", re.IGNORECASE)
    from policy_check.rules._secret_scan_config import resolve_markers

    return engine._build_marker_re(resolve_markers(_config()))


def test_guarded_file_is_still_the_only_reason_for_its_allow_entry() -> None:
    allow = (_config().get("secret_scan") or {}).get("allow") or []
    assert GUARDED in allow, "防線對象須與 allow 條目一致；移除 allow 時一併移除本測試"


def test_pinned_patterns_match_engine_when_available() -> None:
    engine = _engine()
    if engine is None:
        return  # CI 無 engine：釘選副本即權威，漂移由有 engine 的本機 policy_check 執行時抓出
    for name, (src, flags) in _PINNED.items():
        pattern = getattr(engine, name)
        assert (pattern.pattern, pattern.flags & re.IGNORECASE) == (src, flags & re.IGNORECASE), name


def test_allowed_oracle_review_has_no_credential_or_marker_and_single_structural_hit() -> None:
    patterns = _patterns()
    marker_re = _marker_re()
    structural: list[int] = []
    credential: list[int] = []
    marker: list[int] = []
    text = (ROOT / GUARDED).read_text(encoding="utf-8", errors="replace")
    for ln, line in enumerate(text.splitlines(), start=1):
        if patterns["_STRUCTURAL"].search(line) or patterns["_PRIVATE_KEY"].search(line):
            structural.append(ln)
        if patterns["_AWS_ACCESS_KEY"].search(line) or patterns["_GITHUB_TOKEN"].search(line):
            credential.append(ln)
        if marker_re is not None and marker_re.search(line):
            marker.append(ln)
    assert credential == [], f"整檔豁免下不得出現 credential 樣式：行 {credential}"
    assert marker == [], f"整檔豁免下不得出現 marker 樣式：行 {marker}"
    assert structural == EXPECTED_STRUCTURAL_LINES, f"structural 命中須恰為雲端容器路徑所在行：{structural}"
