"""P4-T18：windows-task Spike 2 程序、腳本與證據 schema 測試。

對應 `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` §Task 18。

被測介面：
- `scripts/rust/judge_spike2.py --evidence-dir <dir>`：讀取 `<dir>/*.json` 的合成證據，
  stdout 須含判定結果——JSON 頂層 `verdict` 欄，或單獨出現的 PASS／FAIL／INSUFFICIENT_EVIDENCE
  關鍵字（二者擇一，且只能出現一種）。
- `scripts/rust/win/spike2_windows_task.ps1 -Phase <phase> -InstallRoot <p> -OutDir <p> -TaskName <n> -WhatIf`：
  `-WhatIf` 時將會執行的 `schtasks` 命令逐行輸出到 stdout（計畫 OD-15 寫法）。
- `scripts/rust/win/collect_task_evidence.ps1`：與上列 .ps1 一併做破壞性指令掃描。
- `docs/rust-refactor/deploy/` 下三份 deploy 文件含「前置條件／步驟／預期證據／判定／清理」五節。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 模組層只放平台與資源 marker（tests/test_resource_markers.py 稽核規則）
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
JUDGE = ROOT / "scripts/rust/judge_spike2.py"
SPIKE_PS1 = ROOT / "scripts/rust/win/spike2_windows_task.ps1"
COLLECT_PS1 = ROOT / "scripts/rust/win/collect_task_evidence.ps1"
PS1_FILES = (SPIKE_PS1, COLLECT_PS1)
DEPLOY_DOCS = (
    ROOT / "docs/rust-refactor/deploy/spike2-windows-task.md",
    ROOT / "docs/rust-refactor/deploy/windows-task-restart-procedure.md",
    ROOT / "docs/rust-refactor/deploy/windows-remote-live-e2e.md",
)
DEPLOY_SECTIONS = ("前置條件", "步驟", "預期證據", "判定", "清理")

MISSING = "產物尚未交付"

THROWAWAY_TASK = "serialwrap-throwaway-20261019T000000Z-a1b2c3"
THROWAWAY_TASK_R1 = "serialwrap-throwaway-r1"
FORMAL_TASK = "serialwrap"
OWNER_USER = "OWNER-HOST\\owner"  # 占位身分，非真實帳號
T0 = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)
HEX64 = "ab" * 32

# 計畫 Task 18 Interfaces：證據 schema 必填欄位（十項，不含計畫未列的 task_name／evidence_layer）
REQUIRED_FIELDS = (
    "case_id",
    "artifact_sha256",
    "machine_id",
    "os_build",
    "user",
    "started_at",
    "ended_at",
    "observations",
    "task_xml_sha256",
    "verdict_inputs",
)

VERDICT_RE = re.compile(r"\b(PASS|FAIL|INSUFFICIENT_EVIDENCE)\b")
DESTRUCTIVE_RE = re.compile(
    r"\b(reg(?:\.exe)?\s+delete|sc(?:\.exe)?\s+delete|Format-Volume|Unregister-ScheduledTask"
    r"|rd\s+/s|rmdir\s+/s|del\s+/[sqf])\b",
    re.IGNORECASE,
)


# ---------- 合成證據建構（判定器輸入） ----------

def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _evidence(case_id, *, started, ended, observations, task_name=THROWAWAY_TASK,
              layer="host-throwaway", user=OWNER_USER, artifact_sha256=None,
              task_xml_sha256=HEX64, verdict_inputs=None):
    if artifact_sha256 is None:
        artifact_sha256 = {"cli": HEX64, "launcher": HEX64, "runtime": HEX64}
    if verdict_inputs is None:
        verdict_inputs = {
            "execution_time_limit": "PT0S",
            "instances_policy": "IgnoreNew",
            "restart_on_failure": "PT1M",
        }
    return {
        "case_id": case_id,
        "evidence_layer": layer,
        "task_name": task_name,
        "user": user,
        "artifact_sha256": artifact_sha256,
        "machine_id": "synthetic-host",
        "os_build": "10.0.26100",
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "task_xml_sha256": task_xml_sha256,
        "observations": observations,
        "verdict_inputs": verdict_inputs,
    }


def _longrun(hours: float, *, gap_h=None, task_name=THROWAWAY_TASK):
    """win-task-72h 合成證據：每 5 分鐘一筆 health_ping（ok）；gap_h=(起, 迄) 小時區間內的取樣整段缺漏。"""
    total_min = round(hours * 60)
    obs = []
    for m in range(0, total_min + 1, 5):
        if gap_h is not None and gap_h[0] * 60 <= m < gap_h[1] * 60:
            continue
        obs.append({"ts": _iso(T0 + timedelta(minutes=m)), "kind": "health_ping", "value": "ok"})
    return _evidence("win-task-72h", started=T0, ended=T0 + timedelta(hours=hours),
                     observations=obs, task_name=task_name)


def _restart(seconds: float):
    """win-task-restart-on-failure 合成證據：單筆 relaunch_seconds。"""
    return _evidence("win-task-restart-on-failure", started=T0, ended=T0 + timedelta(minutes=10),
                     observations=[{"ts": _iso(T0 + timedelta(minutes=5)), "kind": "relaunch_seconds",
                                    "value": seconds}],
                     verdict_inputs={"expected_max_seconds": 90.0, "old_pid": 1000, "new_pid": 2000})


def _single(counts):
    """win-task-single-instance 合成證據：每分鐘一筆 serialwrapd_instances。"""
    obs = [{"ts": _iso(T0 + timedelta(minutes=i)), "kind": "serialwrapd_instances", "value": c}
           for i, c in enumerate(counts)]
    return _evidence("win-task-single-instance", started=T0,
                     ended=T0 + timedelta(minutes=len(counts)), observations=obs,
                     verdict_inputs={"concurrency_runs": 2})


def _write_evidence(d: Path, doc: dict) -> Path:
    # 檔名固定：schema 缺欄位測試會刪除 case_id，不能以它命名
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d


# ---------- 判定器呼叫 ----------

def _judge(evidence_dir: Path, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    assert JUDGE.is_file(), f"scripts/rust/judge_spike2.py {MISSING}"
    cmd = [sys.executable, str(JUDGE), "--evidence-dir", str(evidence_dir)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=120, check=False)


def _verdict(proc: subprocess.CompletedProcess) -> str | None:
    out = proc.stdout
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("verdict"), str):
        return doc["verdict"]
    found = set(VERDICT_RE.findall(out))
    return found.pop() if len(found) == 1 else None


def _expect(proc: subprocess.CompletedProcess, expected: str) -> None:
    got = _verdict(proc)
    assert got == expected, (f"判定應為 {expected}，實際 {got!r}\n"
                             f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}")


# ---------- 判定器測試 ----------

@pytest.mark.case("win-task-72h")
def test_judge_72h_continuous_sampling_passes(tmp_path):
    # 72 h 足夠且取樣連續 → PASS
    _expect(_judge(_write_evidence(tmp_path / "pass", _longrun(72.0))), "PASS")


@pytest.mark.case("win-task-72h")
def test_judge_71_9h_runtime_fails(tmp_path):
    # 71.9 h → FAIL（取樣本身連續，缺的是時長）
    _expect(_judge(_write_evidence(tmp_path / "short", _longrun(71.9))), "FAIL")


@pytest.mark.case("win-task-72h")
def test_judge_sample_gap_2h_insufficient(tmp_path):
    # 取樣缺 2 小時 → INSUFFICIENT_EVIDENCE（時長已超過 72 h，缺口是唯一原因）
    _expect(_judge(_write_evidence(tmp_path / "gap", _longrun(73.0, gap_h=(30.0, 32.0)))),
            "INSUFFICIENT_EVIDENCE")


def test_judge_72h_zero_healthy_samples_insufficient(tmp_path):
    # 72 h 無任何 health_ping 取樣（皆為 unrelated）→ INSUFFICIENT_EVIDENCE
    obs = [{"ts": _iso(T0 + timedelta(minutes=m)), "kind": "unrelated", "value": "failed"}
           for m in range(0, round(72.0 * 60) + 1, 5)]
    doc = _evidence("win-task-72h", started=T0, ended=T0 + timedelta(hours=72.0), observations=obs)
    _expect(_judge(_write_evidence(tmp_path / "no_health", doc)), "INSUFFICIENT_EVIDENCE")


@pytest.mark.case("win-task-restart-on-failure")
@pytest.mark.case("p1-rst-daemon")
def test_judge_restart_120s_fails(tmp_path):
    # 重啟 120 s → FAIL（期望 ≤ 90 s）；對照組 45 s → PASS
    _expect(_judge(_write_evidence(tmp_path / "slow", _restart(120))), "FAIL")
    _expect(_judge(_write_evidence(tmp_path / "fast", _restart(45))), "PASS")


@pytest.mark.case("win-task-single-instance")
def test_judge_single_instance_count_2_fails(tmp_path):
    # 實例數出現 2 → FAIL；對照組恆為 1 → PASS
    _expect(_judge(_write_evidence(tmp_path / "dup", _single([1, 1, 2, 1]))), "FAIL")
    _expect(_judge(_write_evidence(tmp_path / "one", _single([1, 1, 1, 1]))), "PASS")


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_judge_schema_missing_field_insufficient(tmp_path, field):
    # schema 必填欄位缺漏 → INSUFFICIENT_EVIDENCE（逐欄位驗）
    doc = _longrun(72.0)
    del doc[field]
    _expect(_judge(_write_evidence(tmp_path / "missing", doc)), "INSUFFICIENT_EVIDENCE")


def test_judge_zero_hash_insufficient(tmp_path):
    # 全零雜湊視為偽造或未量測證據 → INSUFFICIENT_EVIDENCE
    doc = _longrun(72.0)
    doc["artifact_sha256"]["cli"] = "0" * 64
    _expect(_judge(_write_evidence(tmp_path / "zero_hash", doc)), "INSUFFICIENT_EVIDENCE")


def test_judge_unknown_case_id_insufficient(tmp_path):
    # 未知案例或未定義規則之案例 → INSUFFICIENT_EVIDENCE
    doc = _evidence("totally-made-up-case", started=T0, ended=T0 + timedelta(hours=1),
                    observations=[{"ts": _iso(T0), "kind": "dummy", "value": "test"}])
    _expect(_judge(_write_evidence(tmp_path / "unknown_case", doc)), "INSUFFICIENT_EVIDENCE")


def test_judge_require_case_missing_insufficient(tmp_path):
    # 指定 --require-case win-task-72h 但目錄內缺少該案 → INSUFFICIENT_EVIDENCE
    p = _write_evidence(tmp_path / "only_single", _single([1, 1]))
    res = _judge(p, extra_args=["--require-case", "win-task-72h"])
    _expect(res, "INSUFFICIENT_EVIDENCE")


def test_judge_emit_p4_evidence(tmp_path):
    # --emit-p4-evidence 寫入符合 check_p4_cases.py 的 records 格式
    p = _write_evidence(tmp_path / "ev", _restart(45.0))
    p4_dir = tmp_path / "p4_out"
    res = _judge(p, extra_args=["--emit-p4-evidence", str(p4_dir)])
    _expect(res, "PASS")
    out_file = p4_dir / "win-task-restart-on-failure.json"
    assert out_file.is_file(), f"未產生 P4 證據檔: {out_file}"
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "records" in data and len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["round"] == "rust" and rec["platform"] == "windows" and rec["result"] == "PASS"


def test_judge_formal_task_name_not_judged(tmp_path):
    # task_name 不以 serialwrap-throwaway- 開頭、且屬正式工作（同一擁有者身分）→ 不判定
    _expect(_judge(_write_evidence(tmp_path / "formal", _longrun(72.0, task_name=FORMAL_TASK))),
            "INSUFFICIENT_EVIDENCE")
    # 對照組：同樣內容、throwaway 前綴 → PASS（證明拒絕來自工作名，不是內容）
    _expect(_judge(_write_evidence(tmp_path / "throwaway", _longrun(72.0, task_name=THROWAWAY_TASK))), "PASS")


# ---------- PowerShell 腳本測試（靜態＋WhatIf 動態） ----------

def _pwsh_cmd() -> list[str] | None:
    exe = shutil.which("pwsh")
    if not exe and sys.platform == "win32":
        exe = shutil.which("powershell.exe")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SPIKE_PS1)] if exe else None


def test_ps1_files_utf8_bom_or_ascii():
    """驗證 .ps1 腳本帶 UTF-8 BOM 或為純 ASCII，以確保 Windows PowerShell 5.1 解析無誤。"""
    for p in PS1_FILES:
        assert p.is_file(), f"{p.relative_to(ROOT)} {MISSING}"
        data = p.read_bytes()
        has_bom = data.startswith(b"\xef\xbb\xbf")
        is_ascii = False
        try:
            data.decode("ascii")
            is_ascii = True
        except UnicodeDecodeError:
            pass
        assert has_bom or is_ascii, f"{p.name} 必須為 UTF-8 with BOM 或純 ASCII（供 Windows PowerShell 5.1 解析）"


@pytest.mark.case("win-task-logon-trigger")
@pytest.mark.case("win-task-single-instance")
@pytest.mark.case("win-task-restart-on-failure")
def test_whatif_schtasks_only_throwaway_task_name(tmp_path):
    assert SPIKE_PS1.is_file(), f"scripts/rust/win/spike2_windows_task.ps1 {MISSING}"
    src = SPIKE_PS1.read_text(encoding="utf-8")
    # 靜態：原始碼不得把正式工作名寫成 /TN 字面值（OD-15：絕不碰正式 serialwrap 工作）
    assert not re.search(r"/TN\s+[\"']?serialwrap[\"']?(?=\s|$)", src, re.IGNORECASE | re.MULTILINE), \
        "原始碼含 /TN serialwrap 字面值（正式工作名）"
    # 證據層：throwaway 執行產出的證據須標 evidence_layer = host-throwaway
    assert "host-throwaway" in src, "原始碼未寫入 evidence_layer 'host-throwaway'"

    cmd = _pwsh_cmd()
    if cmd is None:
        pytest.skip("無 pwsh 或 powershell.exe：-WhatIf 動態斷言證據不足（非 PASS）")
    for phase in ("install", "single", "restart", "longrun-start"):
        proc = subprocess.run(
            [*cmd, "-Phase", phase, "-InstallRoot", str(tmp_path / "install"),
             "-OutDir", str(tmp_path / "out"), "-TaskName", THROWAWAY_TASK_R1, "-WhatIf"],
            capture_output=True, text=True, cwd=ROOT, timeout=120, check=False)
        lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if re.search(r"schtasks", ln, re.I)]
        if phase in ("install", "single"):
            assert lines, f"{phase}：-WhatIf 未輸出任何 schtasks 命令"
        for ln in lines:
            tns = [m.group(1).strip("\"'") for m in re.finditer(r"/TN\s+(\"[^\"]*\"|'[^']*'|\S+)", ln, re.IGNORECASE)]
            assert tns, f"{phase}：schtasks 命令缺 /TN：{ln!r}"
            assert all(t == THROWAWAY_TASK_R1 for t in tns), \
                f"{phase}：schtasks 命令 /TN 非 {THROWAWAY_TASK_R1}：{ln!r}"


@pytest.mark.case("win-task-logon-trigger")
def test_ps1_destructive_commands_limited_to_install_root():
    for p in PS1_FILES:
        assert p.is_file(), f"{p.relative_to(ROOT)} {MISSING}"
    violations = []
    for p in PS1_FILES:
        for no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bRemove-Item\b", code, re.IGNORECASE) and re.search(r"-Recurse\b", code, re.IGNORECASE):
                if not re.search(r"\$\{?InstallRoot\}?", code):
                    violations.append(f"{p.name}:{no}: Remove-Item -Recurse 指向 install root 以外：{line.strip()}")
            if DESTRUCTIVE_RE.search(code):
                violations.append(f"{p.name}:{no}: 非 schtasks /Delete 的破壞性指令：{line.strip()}")
    assert not violations, "\n".join(violations)


# ---------- deploy 文件結構 ----------

def _headings(text: str) -> list[str]:
    """回傳 Markdown 標題行（略過 ``` 程式碼區塊內的 # 註解）。"""
    out, in_fence = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and ln.lstrip().startswith("#"):
            out.append(ln)
    return out


@pytest.mark.case("win-task-logon-trigger")
@pytest.mark.case("p1-rst-daemon")
@pytest.mark.case("rm-live-e2e")
def test_deploy_docs_have_five_sections():
    for doc in DEPLOY_DOCS:
        assert doc.is_file(), f"{doc.relative_to(ROOT)} {MISSING}"
        heads = _headings(doc.read_text(encoding="utf-8"))
        for sec in DEPLOY_SECTIONS:
            assert any(sec in h for h in heads), f"{doc.name} 缺「{sec}」節（標題）"
