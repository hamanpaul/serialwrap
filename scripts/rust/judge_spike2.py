#!/usr/bin/env python3
"""Spike 2 Windows Task 證據判定器（scripts/rust/judge_spike2.py）。

依據 contracts/evidence/spike2_windows_task.schema.json 驗證證據格式，
並針對 win-task-72h、win-task-restart-on-failure、win-task-single-instance、
win-task-logon-trigger、p1-rst-daemon 與 rm-live-e2e 等案例判定
PASS、FAIL 或 INSUFFICIENT_EVIDENCE。
符合規格 §11.4 可追溯性與 OD-15 隔離工作要求。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

# 專案根目錄與 Schema 定義位置
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = REPO_ROOT / "contracts" / "evidence" / "spike2_windows_task.schema.json"

MAX_ALLOWED_SAMPLE_GAP_SECONDS = 15 * 60  # 取樣間隔上限 15 分鐘（超過視為缺漏）
RESTART_THRESHOLD_SECONDS = 90.0          # 重啟容許上限 90 秒（PT1M＋啟動時間）
LONGRUN_HOURS_THRESHOLD = 72.0            # 72 小時連續運行門檻
ZERO_SHA256 = "0" * 64
HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

# 載入 JSON Schema
_SCHEMA_VALIDATOR: jsonschema.Draft202012Validator | None = None


def _get_schema_validator() -> jsonschema.Draft202012Validator:
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        if not SCHEMA_FILE.is_file():
            raise FileNotFoundError(f"找不到 Schema 檔案: {SCHEMA_FILE}")
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        _SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(schema)
    return _SCHEMA_VALIDATOR


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _get_git_head() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _parse_iso(ts_str: str) -> datetime | None:
    """解析 ISO 8601 時間字串。"""
    try:
        if not isinstance(ts_str, str):
            return None
        s = ts_str
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _validate_schema(doc: dict[str, Any]) -> tuple[bool, str]:
    """驗證文件欄位是否符合契約 Schema。"""
    if not isinstance(doc, dict):
        return False, "證據頂層必須為物件"

    try:
        validator = _get_schema_validator()
        errors = list(validator.iter_errors(doc))
        if errors:
            first_err = errors[0]
            path = ".".join(str(p) for p in first_err.path) or "root"
            return False, f"Schema 驗證失敗 ({path}): {first_err.message}"
    except Exception as e:
        return False, f"Schema 驗證執行錯誤: {e}"

    # 禁止全零雜湊
    artifact = doc.get("artifact_sha256")
    if isinstance(artifact, dict):
        for k in ("cli", "launcher", "runtime"):
            val = str(artifact.get(k) or "")
            if val == ZERO_SHA256:
                return False, f"artifact_sha256.{k} 不得為全零雜湊"

    if str(doc.get("task_xml_sha256") or "") == ZERO_SHA256:
        return False, "task_xml_sha256 不得為全零雜湊"

    return True, "ok"


def judge_single_evidence(
    doc: dict[str, Any], formal_user: str | None = None
) -> tuple[str, str]:
    """針對單份證據進行判定。

    回傳: (verdict, reason)
    """
    valid, msg = _validate_schema(doc)
    if not valid:
        return "INSUFFICIENT_EVIDENCE", msg

    task_name = str(doc.get("task_name", ""))
    evidence_layer = str(doc.get("evidence_layer", ""))
    user = str(doc.get("user", ""))

    # OD-15 限制：throwaway 執行產出的證據其 task_name 必須以 serialwrap-throwaway- 開頭，
    # 若為正式工作名且屬正式工作身分，非特別授權下不判定以保護生產環境
    is_throwaway = task_name.startswith("serialwrap-throwaway-")
    if not is_throwaway:
        if formal_user and user == formal_user:
            pass  # 正式授權環境執行之評估
        else:
            return (
                "INSUFFICIENT_EVIDENCE",
                f"task_name 非 throwaway 工作名且未獲正式身分授權: {task_name}",
            )

    case_id = str(doc.get("case_id", ""))
    started_dt = _parse_iso(str(doc.get("started_at", "")))
    ended_dt = _parse_iso(str(doc.get("ended_at", "")))
    if started_dt is None or ended_dt is None:
        return "INSUFFICIENT_EVIDENCE", "時間戳解析失敗"

    observations: list[dict[str, Any]] = doc.get("observations", [])
    verdict_inputs: dict[str, Any] = doc.get("verdict_inputs", {})

    # 1. win-task-72h：要求運行時長 ≥ 72 h 且取樣連續（每 5 分鐘 ok）、無缺口
    if case_id == "win-task-72h":
        if not observations:
            return "INSUFFICIENT_EVIDENCE", "無取樣觀測值"

        # 必須包含 health_ping 觀測值
        health_obs: list[tuple[datetime, dict[str, Any]]] = []
        for obs in observations:
            if not isinstance(obs, dict):
                return "INSUFFICIENT_EVIDENCE", "觀測值項目不是物件"
            if obs.get("kind") == "health_ping":
                ts = _parse_iso(str(obs.get("ts", "")))
                if ts is None:
                    return "INSUFFICIENT_EVIDENCE", "觀測值時間戳格式無效"
                health_obs.append((ts, obs))

        if not health_obs:
            return "INSUFFICIENT_EVIDENCE", "缺少 health_ping 觀測值"

        health_obs.sort(key=lambda x: x[0])

        # 檢查起點到第一筆、各相鄰 health_ping 取樣點、最後一筆到結束的時間間隔
        if (health_obs[0][0] - started_dt).total_seconds() > MAX_ALLOWED_SAMPLE_GAP_SECONDS:
            return "INSUFFICIENT_EVIDENCE", "起始 health_ping 取樣缺漏"

        for i in range(len(health_obs) - 1):
            gap = (health_obs[i + 1][0] - health_obs[i][0]).total_seconds()
            if gap > MAX_ALLOWED_SAMPLE_GAP_SECONDS:
                return "INSUFFICIENT_EVIDENCE", f"取樣中斷缺漏（間隔 {gap:.1f} 秒 > 門檻）"

        if (ended_dt - health_obs[-1][0]).total_seconds() > MAX_ALLOWED_SAMPLE_GAP_SECONDS:
            return "INSUFFICIENT_EVIDENCE", "結尾 health_ping 取樣缺漏"

        # 檢查健康取樣結果
        for _, obs in health_obs:
            if obs.get("value") != "ok":
                return "FAIL", f"health_ping 非 ok: {obs.get('value')}"

        # 檢查總時長
        duration_hours = (ended_dt - started_dt).total_seconds() / 3600.0
        if duration_hours < LONGRUN_HOURS_THRESHOLD:
            return "FAIL", f"總運行時長不足 72 小時: {duration_hours:.2f} h"

        # 檢查 Task XML 核心設定：ExecutionTimeLimit 必須為 PT0S
        exec_limit = verdict_inputs.get("execution_time_limit")
        if exec_limit != "PT0S":
            return "FAIL", f"execution_time_limit 非 PT0S（實際為 {exec_limit}）"

        # 檢查 daemon 身分一致性（若有取樣 PID 或 started_at，中途不得變更）
        pids = {obs.get("pid") for _, obs in health_obs if "pid" in obs}
        if len(pids) > 1:
            return "FAIL", f"72 小時期間 daemon pid 發生非預期重啟/切換: {pids}"

        return "PASS", f"72 小時長跑取樣連續且皆 ok（時長: {duration_hours:.2f} h）"

    # 2. win-task-restart-on-failure：模擬崩潰後重新拉起時間 ≤ 90 秒
    elif case_id == "win-task-restart-on-failure":
        # 檢查是否有逾時紀錄
        timeout_obs = [
            obs for obs in observations if obs.get("kind") == "relaunch_timeout"
        ]
        if timeout_obs:
            return "FAIL", "模擬崩潰後重啟逾時，daemon 未能拉起"

        relaunch_values = [
            obs.get("value")
            for obs in observations
            if obs.get("kind") == "relaunch_seconds"
        ]
        if not relaunch_values:
            return "INSUFFICIENT_EVIDENCE", "缺少 relaunch_seconds 觀測值"

        for val in relaunch_values:
            if val is None or not isinstance(val, (int, float)):
                try:
                    sec = float(val)
                except (ValueError, TypeError):
                    return "INSUFFICIENT_EVIDENCE", f"relaunch_seconds 數值解析失敗: {val}"
            else:
                sec = float(val)

            if not math.isfinite(sec) or sec <= 0:
                return "INSUFFICIENT_EVIDENCE", f"relaunch_seconds 必須為正有限數: {sec}"
            if sec > RESTART_THRESHOLD_SECONDS:
                return "FAIL", f"重新拉起時間過長: {sec} s（上限 {RESTART_THRESHOLD_SECONDS} s）"

        # 若紀錄了 old_pid 與 new_pid，斷言 PID 改變
        old_pid = verdict_inputs.get("old_pid")
        new_pid = verdict_inputs.get("new_pid")
        if old_pid is not None and new_pid is not None and old_pid == new_pid:
            return "FAIL", f"重啟前後 PID 未變動（舊 {old_pid}，新 {new_pid}）"

        return "PASS", f"重新拉起時間符合要求（皆 ≤ {RESTART_THRESHOLD_SECONDS} s）"

    # 3. win-task-single-instance：多重 schtasks /Run 下實例數恆為 1
    elif case_id == "win-task-single-instance":
        instance_counts = [
            obs.get("value")
            for obs in observations
            if obs.get("kind") == "serialwrapd_instances"
        ]
        if not instance_counts:
            return "INSUFFICIENT_EVIDENCE", "缺少 serialwrapd_instances 觀測值"

        concurrency_runs = verdict_inputs.get("concurrency_runs", 1)
        if len(instance_counts) < 2 and concurrency_runs < 2:
            return "INSUFFICIENT_EVIDENCE", "並發觸發次數不足 2 次"

        for count in instance_counts:
            if type(count) is not int:
                try:
                    if isinstance(count, float) and not count.is_integer():
                        return "FAIL", f"實例數非整數: {count}"
                    cnt = int(count)
                except (ValueError, TypeError):
                    return "INSUFFICIENT_EVIDENCE", f"serialwrapd_instances 解析失敗: {count}"
            else:
                cnt = count

            if cnt != 1:
                return "FAIL", f"實例數非 1: {cnt}"

        return "PASS", "實例數恆為 1"

    # 4. win-task-logon-trigger：登入自啟與 XML 屬性檢驗
    elif case_id == "win-task-logon-trigger":
        if not observations:
            return "INSUFFICIENT_EVIDENCE", "缺少登入觸發觀測值"

        # 檢驗 XML 核心屬性
        exec_limit = verdict_inputs.get("execution_time_limit")
        instances_policy = verdict_inputs.get("instances_policy")
        restart_policy = verdict_inputs.get("restart_on_failure")
        if exec_limit != "PT0S":
            return "FAIL", f"Task XML execution_time_limit 非 PT0S: {exec_limit}"
        if instances_policy != "IgnoreNew":
            return "FAIL", f"Task XML instances_policy 非 IgnoreNew: {instances_policy}"
        if not restart_policy or "PT1M" not in str(restart_policy):
            return "FAIL", f"Task XML restart_on_failure 缺少 PT1M: {restart_policy}"

        # 檢驗健康探測
        has_health_ok = False
        for obs in observations:
            if obs.get("kind") == "health_ping":
                if obs.get("value") != "ok":
                    return "FAIL", f"登入自啟後 health_ping 非 ok: {obs.get('value')}"
                has_health_ok = True

        if not has_health_ok:
            return "INSUFFICIENT_EVIDENCE", "缺少登入自啟後 health_ping: ok 紀錄"

        return "PASS", "登入自啟與 XML 配置驗證成功"

    # 5. p1-rst-daemon：Windows Task 監管重啟面
    elif case_id == "p1-rst-daemon":
        if not observations:
            return "INSUFFICIENT_EVIDENCE", "缺少 p1-rst-daemon 重啟觀測值"

        has_relaunch = False
        for obs in observations:
            if obs.get("kind") == "relaunch_seconds":
                sec = float(obs.get("value", 0))
                if sec > RESTART_THRESHOLD_SECONDS:
                    return "FAIL", f"p1-rst-daemon 重啟逾時: {sec} s"
                has_relaunch = True
            elif obs.get("kind") == "health_ping" and obs.get("value") != "ok":
                return "FAIL", f"p1-rst-daemon 重啟後 health_ping 失敗: {obs.get('value')}"
            elif obs.get("kind") == "two_reader_instances" and int(obs.get("value", 0)) > 1:
                return "FAIL", f"p1-rst-daemon 出現雙重實例衝突: {obs.get('value')}"

        if not has_relaunch:
            return "INSUFFICIENT_EVIDENCE", "缺少 relaunch_seconds 觀測值"

        return "PASS", "p1-rst-daemon 重啟與狀態恢復驗證通過"

    # 6. rm-live-e2e：Windows 遠端端到端通道驗證面
    elif case_id == "rm-live-e2e":
        if not observations:
            return "INSUFFICIENT_EVIDENCE", "缺少 rm-live-e2e 觀測值"

        has_ping = False
        has_cmd = False
        for obs in observations:
            kind = obs.get("kind")
            val = obs.get("value")
            if kind == "remote_ping" and val == "ok":
                has_ping = True
            elif kind == "cmd_status" and val == "completed":
                has_cmd = True
            elif kind == "wire_corruption" and val:
                return "FAIL", f"遠端位元組傳輸毀損或亂序: {val}"
            elif kind == "orphan_process" and val:
                return "FAIL", f"清理後殘留孤兒程序: {val}"

        if not has_ping or not has_cmd:
            return "INSUFFICIENT_EVIDENCE", "缺少 remote_ping 或 cmd_status 觀測值"

        return "PASS", "rm-live-e2e 遠端通道與命令執行驗證通過"

    # 7. 未知或未定義規則之案例
    else:
        return "INSUFFICIENT_EVIDENCE", f"未知案例或未定義判定規則: {case_id}"


def _emit_p4_evidence(
    emit_dir: Path,
    case_id: str,
    commit: str,
    artifact_path: str,
    artifact_sha256: str,
) -> None:
    """寫入符合 check_p4_cases.py 規範的 target/p4-evidence/<case_id>.json。"""
    emit_dir.mkdir(parents=True, exist_ok=True)
    out_file = emit_dir / f"{case_id}.json"

    existing_records: list[dict[str, Any]] = []
    if out_file.is_file():
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
            if isinstance(data.get("records"), list):
                existing_records = data["records"]
        except Exception:
            existing_records = []

    # 移除同 commit / platform / round 的舊記錄
    filtered = [
        r
        for r in existing_records
        if not (
            r.get("commit") == commit
            and r.get("platform") == "windows"
            and r.get("round") == "rust"
        )
    ]

    new_record = {
        "case_id": case_id,
        "round": "rust",
        "platform": "windows",
        "commit": commit,
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_sha256,
        "result": "PASS",
    }
    filtered.append(new_record)

    doc = {"case_id": case_id, "records": filtered}
    out_file.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Spike 2 Windows Task 證據判定器")
    parser.add_argument(
        "--evidence-dir", required=True, type=Path, help="存放證據 JSON 的目錄"
    )
    parser.add_argument(
        "--require-case",
        action="append",
        default=[],
        help="必須具備之案例 ID（缺漏則判定 INSUFFICIENT_EVIDENCE）",
    )
    parser.add_argument(
        "--formal-user",
        type=str,
        default=None,
        help="正式工作使用者身分（啟用非 throwaway 判定）",
    )
    parser.add_argument(
        "--emit-p4-evidence",
        type=Path,
        default=None,
        help="將 PASS 結果寫入指定之 P4 案例驗證目錄（如 target/p4-evidence）",
    )
    args = parser.parse_args()

    evidence_dir: Path = args.evidence_dir
    if not evidence_dir.is_dir():
        out = {
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reason": f"目錄不存在: {evidence_dir}",
            "evaluated_files": 0,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 2

    json_files = sorted(evidence_dir.glob("*.json"))
    if not json_files:
        out = {
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reason": "目錄內無任何 JSON 證據檔",
            "evaluated_files": 0,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 2

    results: list[dict[str, Any]] = []
    has_fail = False
    has_insufficient = False
    evaluated_cases: set[str] = set()

    git_head = _get_git_head()

    for file_path in json_files:
        try:
            content = file_path.read_text(encoding="utf-8-sig")
            doc = json.loads(content)
            file_sha = _sha256_bytes(content.encode("utf-8"))
        except Exception as e:
            results.append({
                "file": file_path.name,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reason": f"讀取或解析 JSON 失敗: {e}",
            })
            has_insufficient = True
            continue

        cid = doc.get("case_id")
        if cid:
            evaluated_cases.add(str(cid))

        verdict, reason = judge_single_evidence(doc, formal_user=args.formal_user)

        artifact = doc.get("artifact_sha256", {})
        cli_sha = artifact.get("cli", "") if isinstance(artifact, dict) else ""

        results.append({
            "file": file_path.name,
            "case_id": cid,
            "verdict": verdict,
            "reason": reason,
            "file_sha256": file_sha,
            "artifact_sha256": artifact,
        })

        if verdict == "FAIL":
            has_fail = True
        elif verdict == "INSUFFICIENT_EVIDENCE":
            has_insufficient = True
        elif verdict == "PASS" and args.emit_p4_evidence and cid:
            cli_path = str(
                verdict_inputs_cli := doc.get("verdict_inputs", {}).get(
                    "cli_path", "bin/serialwrap.exe"
                )
            )
            _emit_p4_evidence(
                emit_dir=args.emit_p4_evidence,
                case_id=str(cid),
                commit=git_head,
                artifact_path=cli_path,
                artifact_sha256=cli_sha,
            )

    # 檢查必備案例
    missing_required = [c for c in args.require_case if c not in evaluated_cases]
    if missing_required:
        has_insufficient = True

    if has_fail:
        final_verdict = "FAIL"
        exit_code = 1
    elif has_insufficient:
        final_verdict = "INSUFFICIENT_EVIDENCE"
        exit_code = 2
    else:
        final_verdict = "PASS"
        exit_code = 0

    envelope = {
        "verdict": final_verdict,
        "evaluated_files": len(json_files),
        "missing_required_cases": missing_required,
        "schema_revision": 1,
        "judge_revision": "1.0.0",
        "results": results,
    }
    print(json.dumps(envelope, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
