"""R-21 整檔豁免的防線（雲端審查 d2118cd_be2c4c6 6ad67f1#1 方案 (b)）。

`.project-policy.yml` 的 `secret_scan.allow` 依 policy engine `_is_exempt` 為**整檔豁免**：
放行個人路徑樣式的同時，也停用 credential 與 marker 偵測器。逐字複製的雲端 oracle 審查原文
只為第 3 行的雲端容器工作樹路徑而列入 allow，故本測試以 engine 同一組正規式掃描該檔，要求
credential 與 marker 命中為 0、structural 命中恰為第 3 行一處；日後編輯若夾帶憑證或新路徑即 FAIL。

engine（`policy_check`）不在 CI 的 pytest 依賴內：可匯入時直接取用其正規式並核對本檔釘選副本
與 baseline markers 未漂移；不可匯入時改用釘選副本（仍執行，不 SKIP），marker 集合依 engine
`resolve_markers` 同式由釘選 baseline 與 repo 設定解析，集合為空即 FAIL。
"""

from __future__ import annotations

import re
import sys
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
# 同版 engine `policy_check/data/secret_scan_defaults.yml` 的 baseline 釘選副本（`resolve_markers` 的基底）。
# marker 以字串拆段書寫：本檔不在 allow 內，原字會被 R-21 本身判為 marker 命中。
_PINNED_BASELINE = {
    "markers": ["bgw" "720", "build" "20"],
    "public_names": ["brcm", "broadcom", "airoha", "mtk", "mediatek", "prplos", "prplog", "marvell"],
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


def _pinned_markers(config: dict) -> set[str]:
    # 與 engine `resolve_markers` 同式：(baseline.markers ∪ repo.markers) − (baseline.public_names ∪ repo.public_names)。
    sec = (config or {}).get("secret_scan") or {}

    def repo(key: str) -> set[str]:
        return {str(t).lower() for t in (sec.get(key) or []) if str(t).strip()}

    markers = set(_PINNED_BASELINE["markers"]) | repo("markers")
    public = set(_PINNED_BASELINE["public_names"]) | repo("public_names")
    return markers - public


def _marker_re() -> re.Pattern[str]:
    engine = _engine()
    if engine is None:
        tokens = _pinned_markers(_config())
        if not tokens:
            pytest.fail("marker 集合為空：釘選 baseline 與 repo 設定解析後無任何 marker，防線失效")
        return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(tokens)) + r")\b", re.IGNORECASE)
    from policy_check.rules._secret_scan_config import resolve_markers

    pattern = engine._build_marker_re(resolve_markers(_config()))
    if pattern is None:
        pytest.fail("engine 解析的 marker 集合為空，防線失效")
    return pattern


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
    from policy_check.rules._secret_scan_config import resolve_markers

    assert resolve_markers(_config()) == _pinned_markers(_config()), "baseline markers 釘選副本與 engine 漂移"


def _scan(text: str) -> tuple[list[int], list[int], list[int]]:
    patterns = _patterns()
    marker_re = _marker_re()
    structural: list[int] = []
    credential: list[int] = []
    marker: list[int] = []
    for ln, line in enumerate(text.splitlines(), start=1):
        if patterns["_STRUCTURAL"].search(line) or patterns["_PRIVATE_KEY"].search(line):
            structural.append(ln)
        if patterns["_AWS_ACCESS_KEY"].search(line) or patterns["_GITHUB_TOKEN"].search(line):
            credential.append(ln)
        if marker_re.search(line):
            marker.append(ln)
    return structural, credential, marker


def test_marker_guard_still_catches_baseline_markers_without_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI 無 engine：fallback 須含 engine baseline markers，否則受整檔豁免的檔案夾帶 marker 無任何防線。
    monkeypatch.setattr(sys.modules[__name__], "_engine", lambda: None)
    text = (ROOT / GUARDED).read_text(encoding="utf-8", errors="replace")
    for token in _PINNED_BASELINE["markers"]:
        _, _, marker = _scan(text + f"\nmutant: {token.upper()} build\n")
        assert marker, f"無 engine 時 baseline marker {token} 未被偵測"


def test_allowed_oracle_review_has_no_credential_or_marker_and_single_structural_hit() -> None:
    text = (ROOT / GUARDED).read_text(encoding="utf-8", errors="replace")
    structural, credential, marker = _scan(text)
    assert credential == [], f"整檔豁免下不得出現 credential 樣式：行 {credential}"
    assert marker == [], f"整檔豁免下不得出現 marker 樣式：行 {marker}"
    assert structural == EXPECTED_STRUCTURAL_LINES, f"structural 命中須恰為雲端容器路徑所在行：{structural}"


# OD-33 升級驗證報告原文（`evidence/stage1/upgrade-docker/**`）同為整檔豁免：只為容器內 swtest 測試帳號的
# home 路徑列入 allow。要求 credential／marker 命中為 0，且每一處 home 路徑樣式都只能是該容器帳號。
UPGRADE_EVIDENCE_ALLOW = "evidence/stage1/upgrade-docker/**"
UPGRADE_CONTAINER_HOME = "/home/" "swtest/"  # 拆段書寫：本檔不在 allow 內，原字會被 R-21 判為 structural


def _upgrade_evidence_files() -> list[Path]:
    return sorted(p for p in (ROOT / "evidence/stage1/upgrade-docker").rglob("*") if p.is_file())


def test_upgrade_evidence_allow_entry_matches_guard() -> None:
    allow = (_config().get("secret_scan") or {}).get("allow") or []
    assert UPGRADE_EVIDENCE_ALLOW in allow, "防線對象須與 allow 條目一致；移除 allow 時一併移除本段測試"
    assert _upgrade_evidence_files(), "allow 條目存在但沒有受防線涵蓋的證據檔"


def test_upgrade_evidence_only_container_home_paths() -> None:
    patterns = _patterns()
    for path in _upgrade_evidence_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        _, credential, marker = _scan(text)
        rel = path.relative_to(ROOT).as_posix()
        assert credential == [], f"{rel}：整檔豁免下不得出現 credential 樣式：行 {credential}"
        assert marker == [], f"{rel}：整檔豁免下不得出現 marker 樣式：行 {marker}"
        assert not patterns["_PRIVATE_KEY"].search(text), f"{rel}：不得出現私鑰樣式"
        homes = {m.group(0) for m in patterns["_STRUCTURAL"].finditer(text)}
        assert homes <= {UPGRADE_CONTAINER_HOME}, f"{rel}：只允許容器帳號 home 路徑，實際 {sorted(homes)}"


def test_upgrade_evidence_guard_rejects_host_home_mutant() -> None:
    patterns = _patterns()
    mutant = '{"entry": "/home/' + 'someone/.local/bin/serialwrap"}'
    homes = {m.group(0) for m in patterns["_STRUCTURAL"].finditer(mutant)}
    assert not homes <= {UPGRADE_CONTAINER_HOME}
