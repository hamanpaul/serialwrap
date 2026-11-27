"""T7B 報告資料契約與純資料報告樹產生器。

本檔只用標準函式庫及 T2 的 ``tests/stage3_fakes.py``。所有輸出以
``Path.write_bytes`` 寫入固定 UTF-8／LF 位元組；不啟動 adapter、ingest、daemon、
測試產品程序或實機。每次 run 可獨立設定 verdict、安全不變量、CLI 觀測、artifact
身分、完成狀態及缺少的檔案。

報告資料契約（供 T5B adapter 產生真實資料、供 T7B 讀取）：

1. bridge run 沿用 P2 的
   ``<report-dir>/<case_id>/bridge/<run_index>/``。必要檔為
   ``manifest.json``（``schema_version: "l15-manifest.v1"``）、
   ``comparison.json``（``schema_version: "comparison.v1"``）、
   ``bridge-manifest.json``、``wire.ndjson``、``cli.ndjson``、
   ``daemon.log`` 及 ``wal/``。manifest 的 artifact 身分讀
   ``artifacts.cli.sha256``、``artifacts.daemon.implementation``、
   ``artifacts.daemon.sha256``、Python daemon 的
   ``artifacts.daemon.package_tree_sha256``（正規化為 ``tree_sha256``）及
   ``artifacts.release_manifest_sha256``；daemon 的 ``tree_sha256`` 是 T7B
   讀取視圖欄名，原始 manifest 位元組不改寫。comparison 的 verdict 必須是
   ``PASS`` 或 ``FAIL``；``INSUFFICIENT_EVIDENCE`` 不能成為完整證據。
   P2 comparison schema 的 invariant 欄位實際叫 ``verdict``，T7B 以此讀取；
   若其他 runner 原始格式使用 ``result``，adapter 只在讀取視圖中映射，不產生
   或偽造 ``comparison.json``。``cli.ndjson`` 每筆保留 argv、整數 rc、stdout；
   第 i 筆以 rc 及 stdout JSON 進行比較。wire、CLI、daemon log、WAL 等原始檔
   保持原位元組與 P2 manifest／comparison 所記的 hash。

2. 其他 runner 的 run 目錄為
   ``<report-dir>/<case_id>/<runner>/<run_index>/``，其中 runner 僅為
   ``contract-blackbox``、``windows-native``、``rust-l1``。各目錄只新增
   ``runner-observation.json``（讀取視圖，``schema_version:
   "stage3-runner-observation.v1"``）及 ``source/`` 原始檔，不建立
   ``manifest.json``、``comparison.json`` 或 ``bridge-manifest.json``。
   envelope 必含 ``case_id``、``runner``、``run_index``、
   ``completion: {status: "completed", finished: true, exit_code: <int>}``、
   ``artifacts``、``verdict``、``reason``、``invariants``、
   ``cli_observations``、``source_files``。artifact 身分欄位與 bridge
   manifest 對齊：``artifacts.cli.sha256``、
   ``artifacts.daemon.implementation``、``artifacts.daemon.sha256``、
   ``artifacts.daemon.tree_sha256``、``artifacts.release_manifest_sha256``。
   依 §0.4 契約 3，A／B 的 ``artifacts.cli.sha256`` 必須相等；不符使用
   ``artifact identity mismatch: artifacts.cli.sha256`` 並使完整性失敗。daemon
   implementation／hash 屬當側 release artifact，兩側不要求相等。
   envelope 內 ``verdict`` 映射到 comparison.verdict；每個 invariant 的
   ``{id, result}`` 映射到 comparison.invariants 的 ``{id, verdict}``。
   ``cli_observations`` 依原呼叫順序列出 ``{argv, rc, stdout, source_ref}``；
   每筆 rc 必須是整數、stdout 必須是字串，不存在呼叫記錄不得補空字串。每個
   ``source/release-manifest.json``（現行 release manifest、``schema_version: 1``）
   是當側 CLI／daemon artifact 身分的原始來源；envelope 的
   ``artifacts.release_manifest_sha256`` 必須等於該檔原位元組 SHA-256，其他
   ``artifacts.*`` 欄位由其 ``components.cli``／``components.daemon`` 對應。
   ``source_files`` 項目是 ``{path, sha256}``，path 以 run 目錄為根的相對
   POSIX 路徑，sha256 是原始檔位元組雜湊。``source_ref`` 以側 report-dir
   為根的相對 POSIX 路徑，可定位 envelope 內該觀測或 FAIL 的原始來源。

   - contract-blackbox：原始 ``source/l1-results.json`` 使用現行 P2
     ``schema_version: 2``、``format: "l1-results.v1"``（pytest runner）；
     另列原始 ``source/junit.xml`` 及逐呼叫擷取的
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``（``stage3-invariant-evidence.v1``）。adapter
     verdict／invariants 是讀取視圖，不改寫上述原報告。
   - windows-native：原始 ``source/l1-results.json`` 同為
     ``l1-results.v1``（pytest runner、platform=windows），另必列
     ``source/windows-transport-context.json``（T5B 的
     ``stage3-windows-transport.v1``，含實際 platform、transport、serial
     backend、port_id、是否實際開埠及 context SHA）及
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``。原生 COM／OD-08 實體替代的實際開埠
     證據不得由預定 manifest 值回填；依 R12-01 的去識別與非公開來源規則處理。
   - rust-l1：原始 ``source/l1-results.json`` 使用現行
     ``l1-results.v1``（schema_version=2、runner=cargo；原始報告的
     ``provenance.schema`` 為 ``cargo-l1-provenance.v1``），另列
     ``source/libtest.stdout.txt`` 及
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``。cargo／pytest 本身的通過只證明測試
     runner 完成；只有可對應 required case 的受測產品觀測才能成為 verdict、
     safety 或 CLI 差分來源。

   ``source/cli-observations.ndjson`` 使用 T5B 約定的
   ``stage3-cli-observations.v1`` 記錄，逐行保存呼叫序號、argv、整數 rc 與
   原始 stdout。``source_files`` 必列出該 runner 產生的全部必要來源檔與 SHA；
   loader 必核對檔案存在、SHA 相符、觀測 source_ref 可定位。讀取視圖不修改
   原始報告，不以檔名不同為由略過 verdict／安全結果，不把非 bridge 輸出偽裝
   成 P2 bridge 檔。

3. 缺漏採 fail-closed，缺漏類型不互相代替：缺 required case 為
   ``missing required case: <case_id>``；缺 source_files 所列原檔或 SHA 不符為
   ``missing source file: <relative_path>``／``source hash mismatch:
   <relative_path>``；缺 CLI 記錄、rc 或 stdout 為
   ``missing required observation: cli_observations[<i>].<field>``（例如 argv、rc、stdout）；缺 CLI／daemon
   身分欄位或必要身分值為 ``missing artifact identity: <dotted_field>``；
   envelope 的 status 非 ``completed`` 或 finished 非 true 為
   ``runner incomplete: <runner>``。已完成 runner 的非零 ``exit_code`` 只作執行
   狀態來源，不能單獨推導產品 FAIL；只有有逐案原始 FAIL 來源的 verdict 才進
   ``product_failures``。bridge 缺
   ``manifest.json``、``comparison.json``、``bridge-manifest.json``、
   ``wire.ndjson``、``cli.ndjson``、``daemon.log`` 或 ``wal/``，逐項使用
   ``missing source file: <relative_path>``；P2 manifest 宣告的任一檔案 SHA 不符
   使用 ``source hash mismatch: <relative_path>``。bridge comparison verdict 為
   ``INSUFFICIENT_EVIDENCE`` 時使用
   ``invalid comparison verdict: INSUFFICIENT_EVIDENCE``；
   ``bridge-manifest.json.closed_cleanly`` 非 true 使用
   ``bridge not closed cleanly: bridge-manifest.json.closed_cleanly``；
   ``bridge-manifest.json.failure`` 非 null 使用
   ``bridge failure: bridge-manifest.json.failure``。不能以空 observation 或合成空檔
   當成兩側相同。

4. ``scope: "rust_only"`` 且 ``ab_participation: "absolute_only"`` 的 BC
   不作 A/B 差分；依 B 側絕對驗收 verdict 判定，A 側不同不能產生 A/B difference，
   但 B 側 FAIL 仍須進 ``product_failures`` 並留可核對 source_ref。現行依據是
   ``contracts/behavior_changes.yaml`` 的 ``changes[]`` 欄位
   ``scope``、``ab_participation``、``case_ids``；例如 BC-06 為 rust_only /
   absolute_only，且列出 ``bc06-shutdown-self-test-flush``。

5. 產品／安全 FAIL 先收集，再彙整 integrity；不得因缺案或 runner 未完成提前返回。
   ``product_failures`` 每筆為 ``{side, case, source_ref, reason}``，source_ref
   相對該側 report-dir 且必須指向原始可解析 FAIL 來源。安全 invariant FAIL
   另外保留為 ``<A|B>:<case>:<invariant_id>``；安全 FAIL 不受 B11 欄位正規化
   影響。每筆差異必含 ``source_refs: {a, b}``，分別為兩側觀測的相對來源引用，
   且可定位原始觀測檔／line；不以 report 根絕對路徑作 provenance。

6. B11 暫定觀測範圍：候選規則只允許忽略邏輯命令為 ``daemon status`` 或
   ``health.status``、CLI ``rc == 0`` 且 stdout JSON ``ok is true`` 的回應中最上層
   ``foreign_holders`` 掃描快照。命令不符、非成功回應、巢狀同名欄位、危險 holder、
   ``holder_unknown`` 及任何安全 invariant 都必須保留。本 fixture 的允許正例限於
   ``foreign_holders`` map 只含與回應 ``pid`` 相同的 daemon 自身 PID；含外來 PID
   或 unknown 欄位時，即使位於成功 status 回應最上層也須保留差異。將整個欄位
   無條件刪除而一併掩蓋 unknown／外來 PID，會違反本 Task 測試。此項未經獨立審查
   或擁有者追認，不得凍結為 approved normalization。

這份契約只規範 T7B loader／fixture 與後續 T5B adapter 的讀取介面。它不代表目前
repo 已有非 bridge adapter，也不宣稱純資料 fixture 是產品執行證據。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from stage3_fakes import sha256_file, wal_record


RUNNERS = ("bridge", "contract-blackbox", "windows-native", "rust-l1")
OBSERVATION_SCHEMA = "stage3-runner-observation.v1"
CLI_OBSERVATIONS_SCHEMA = "stage3-cli-observations.v1"
CASE_ID = "l15-fixture-case"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_json(path: Path, value: Any) -> Path:
    return _write_bytes(path, _json_bytes(value))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_identity(side: str) -> dict[str, Any]:
    is_a = side.lower() == "a"
    daemon: dict[str, Any] = {
        "implementation": "python" if is_a else "rust",
        "sha256": "2" * 64 if is_a else "3" * 64,
        "tree_sha256": "4" * 64 if is_a else "5" * 64,
    }
    return {
        "cli": {"sha256": "1" * 64},
        "daemon": daemon,
        "release_manifest_sha256": "6" * 64,
    }


def _merge_mapping(target: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for key, value in overrides.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_mapping(current, value)
        else:
            target[key] = value


def _deployed_content(identity: Mapping[str, Any]) -> dict[str, Any]:
    cli_sha = identity.get("cli", {}).get("sha256")
    daemon_sha = identity.get("daemon", {}).get("sha256")
    return {
        "schema_version": "deployed-content.v1",
        "measured_at": "2026-11-27T00:00:00+00:00",
        "cli_configured": "fixture-cli",
        "cli_resolved": "fixture-cli",
        "cli_sha256": cli_sha,
        "install_dir": "fixture-install",
        "release_manifest_sha256": identity.get("release_manifest_sha256"),
        "bundle_tree_sha256": "a" * 64,
        "runtime_tree_sha256": "b" * 64,
        "entry_dir_sha256": "c" * 64,
        "daemon_configured": ["fixture-daemon"],
        "daemon_resolved": "fixture-daemon",
        "daemon_sha256": daemon_sha,
        "error": None,
    }


def _release_manifest_bytes(identity: Mapping[str, Any]) -> bytes:
    cli = identity.get("cli", {})
    daemon = identity.get("daemon", {})
    return _json_bytes({
        "schema_version": 1,
        "bundle_version": "0.5.1",
        "stage": "3",
        "created_at": "2026-11-27T00:00:00+00:00",
        "target": "fixture-target",
        "git_revision": "0" * 40,
        "contract_revision": "1" * 40,
        "components": {
            "cli": {
                "implementation": "rust",
                "version": "0.5.1",
                "git_revision": "0" * 40,
                "path": "bin/serialwrap",
                "sha256": cli.get("sha256"),
            },
            "daemon": {
                "implementation": daemon.get("implementation"),
                "version": "0.5.1",
                "git_revision": "0" * 40,
                "launcher": {"path": "bin/serialwrapd", "sha256": daemon.get("sha256")},
                "entry": ["bin/serialwrapd"],
                "runtime": {"path": "runtime", "python_version": "3.12", "sha256": "8" * 64},
                "package": {
                    "name": "serialwrap",
                    "version": "0.5.1",
                    "wheel_sha256": None,
                    "tree_sha256": daemon.get("tree_sha256"),
                },
            },
            "python_cli_delegate": {"entry": ["fixture"], "tree_sha256": "9" * 64},
            "assets": {"skill_sha256": "a" * 64, "skill_windows_sha256": "b" * 64, "profiles_sha256": "c" * 64},
            "supervisor": {"default_mode": "on-demand", "definition_sha256": "d" * 64},
        },
        "pairing": {"rule": "exact", "cli_version": "0.5.1", "daemon_version": "0.5.1"},
    })


def _default_call(side: str) -> dict[str, Any]:
    return {
        "argv": ["daemon", "status"],
        "rc": 0,
        "response": {"ok": True, "pid": 4101 if side.lower() == "a" else 4202, "state": "READY"},
    }


def _as_call(call: Mapping[str, Any], side: str) -> dict[str, Any]:
    record: dict[str, Any] = {"argv": list(call.get("argv", ())) }
    if "rc" in call:
        record["rc"] = call["rc"]
    if "stdout" in call:
        record["stdout"] = call["stdout"]
    elif "response" in call:
        record["stdout"] = json.dumps(call["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record.setdefault("stderr", "")
    if "mono_start_ns" in call:
        record["mono_start_ns"] = call["mono_start_ns"]
    return record


def _source_specs(
    runner: str,
    *,
    complete: bool,
    verdict: str,
    invariants: Iterable[Mapping[str, Any]],
    release_manifest: bytes,
    runner_exit_code: int | None,
) -> dict[str, bytes]:
    outcome = "failed" if verdict == "FAIL" else "passed"
    if not complete:
        outcome = "error"
    l1_report = {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "cargo" if runner == "rust-l1" else "pytest",
        "platform": "windows" if runner == "windows-native" else "linux",
        "argv": ["fixture-runner"],
        "markexpr": "",
        "counts": {"all": {"passed": int(outcome == "passed"), "failed": int(outcome == "failed"), "skipped": 0}},
        "resources": {},
        "uart_kind": "native" if runner == "windows-native" else "pty-fake",
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": ["fixture-nodeid"],
        "deselected": [],
        "results": [{
            "nodeid": "fixture-nodeid",
            "outcome": outcome,
            "skip_kind": None,
            "phases": {"setup": "passed", "call": "failed" if outcome == "failed" else "passed", "teardown": "passed"},
        }],
        "exit_code": runner_exit_code if runner_exit_code is not None else (1 if outcome in {"failed", "error"} else 0),
        "finished": bool(complete),
        "deployed_content": None,
    }
    if runner == "rust-l1":
        l1_report["provenance"] = {
            "schema": "cargo-l1-provenance.v1",
            "runner": "cargo",
            "complete": bool(complete),
        }
    specs: dict[str, bytes] = {
        "source/release-manifest.json": release_manifest,
        "source/l1-results.json": _json_bytes(l1_report),
        "source/invariant-evidence.json": _json_bytes({
            "schema_version": "stage3-invariant-evidence.v1",
            "invariants": [dict(item) for item in invariants],
        }),
    }
    if runner in {"contract-blackbox", "windows-native"}:
        failure_xml = '<failure message="fixture failure"/>' if verdict == "FAIL" else ""
        specs["source/junit.xml"] = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<testsuites><testsuite name=\"stage3\" tests=\"1\" "
            f"failures=\"{int(verdict == 'FAIL')}\">"
            f"<testcase name=\"fixture-node\">{failure_xml}</testcase></testsuite></testsuites>\n"
        ).encode("utf-8")
    if runner == "windows-native":
        specs["source/windows-transport-context.json"] = _json_bytes({
            "schema_version": "stage3-windows-transport.v1",
            "platform": "windows",
            "transport": "windows-native-com",
            "serial_backend": "native-com",
            "port_id": "fixture-port",
            "opened": True,
            "candidate_context_sha256": "7" * 64,
        })
    if runner == "rust-l1":
        specs["source/libtest.stdout.txt"] = (
            b"test fixture_node ... FAILED\n" if verdict == "FAIL" else b"test fixture_node ... ok\n"
        )
    return specs


def _source_rows(run_dir: Path, specs: Mapping[str, bytes]) -> list[dict[str, str]]:
    return [
        {"path": rel, "sha256": _sha256(data)}
        for rel, data in sorted(specs.items())
    ]


def write_run(
    report_dir: Path,
    case_id: str,
    *,
    side: str,
    runner: str = "bridge",
    run_index: int = 1,
    verdict: str = "PASS",
    reason: str = "fixture result",
    invariants: Iterable[Mapping[str, Any] | tuple[str, str] | str] = (),
    calls: Iterable[Mapping[str, Any]] | None = None,
    complete: bool = True,
    runner_exit_code: int | None = None,
    missing: Iterable[str] = (),
    identity_missing: Iterable[str] = (),
    artifact_overrides: Mapping[str, Any] | None = None,
    observations: Iterable[Mapping[str, Any]] | None = None,
) -> Path:
    """在 report_dir 建立一筆 run；missing 使用 run 內相對路徑。"""
    if runner not in RUNNERS:
        raise ValueError(f"不支援的 fixture runner：{runner}")
    if verdict not in {"PASS", "FAIL", "INSUFFICIENT_EVIDENCE"}:
        raise ValueError(f"不支援的 fixture verdict：{verdict}")
    root = Path(report_dir)
    run_dir = root / case_id / runner / str(run_index)
    missing_set = set(missing)
    identity = _artifact_identity(side)
    if artifact_overrides:
        _merge_mapping(identity, artifact_overrides)
    for dotted in identity_missing:
        keys = dotted.split(".")
        node: dict[str, Any] = identity
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                break
            node = child
        else:
            node.pop(keys[-1], None)

    normalized_invariants: list[dict[str, Any]] = []
    for invariant in invariants:
        if isinstance(invariant, str):
            normalized_invariants.append({"id": invariant, "result": "PASS"})
        elif isinstance(invariant, tuple):
            normalized_invariants.append({"id": invariant[0], "result": invariant[1]})
        else:
            normalized_invariants.append(dict(invariant))
    if not normalized_invariants:
        normalized_invariants = [{"id": "zero_tx_on_reject", "result": "PASS"}]
    if verdict == "FAIL" and reason == "fixture result":
        reason = "fixture case failed"

    default_calls = [_default_call(side)] if calls is None else list(calls)
    call_rows = [_as_call(call, side) for call in default_calls]
    if observations is not None:
        call_rows = [dict(row) for row in observations]

    if runner == "bridge":
        run_dir.mkdir(parents=True, exist_ok=True)
        bridge_calls: list[dict[str, Any]] = []
        for row in call_rows:
            if "argv" in row:
                argv = list(row["argv"])
                full_argv = ["serialwrap", "--endpoint", f"{side.upper()}_INSTANCE_ROOT/run/serialwrapd.sock", *argv]
                bridge_calls.append({**row, "argv": full_argv})
            else:
                bridge_calls.append(dict(row))
        cli_bytes = b"".join(_json_bytes(row) for row in bridge_calls)
        wire_bytes = _json_bytes({"dir": "TX", "dut_generation": 1, "bytes": 0})
        control_bytes = b"{}\n"
        correlation_bytes = b"{}\n"
        dut_stderr_bytes = b"fixture DUT output\n"
        wal_bytes = _json_bytes(wal_record(1, dir="TX", payload=b""))
        comparison = {
            "schema_version": "comparison.v1",
            "run_id": f"{case_id}-{run_index}",
            "case_id": case_id,
            "verdict": verdict,
            "reasons": [reason] if verdict == "FAIL" else [],
            "tool": {"name": "fixture", "version": "1", "tree_sha256": "6" * 64},
            "integrity": {"wire_records": 1, "wire_sha256": _sha256(wire_bytes),
                          "wire_sha256_expected": _sha256(wire_bytes), "wal_files": ["wal/raw.wal.ndjson"],
                          "wal_records": 1, "problems": []},
            "window": {"until_op": None, "wal_seq": 1, "wire_offsets": {"fixture-port": 0}, "endpoint_generation": 1},
            "streams": [],
            "invariants": [
                {"id": item["id"], "verdict": item["result"], "detail": str(item.get("detail", "fixture invariant"))}
                for item in normalized_invariants
            ],
        }
        files: dict[str, bytes] = {
            "wire.ndjson": wire_bytes,
            "control.ndjson": control_bytes,
            "correlation.ndjson": correlation_bytes,
            "dut.stderr.log": dut_stderr_bytes,
            "cli.ndjson": cli_bytes,
            "daemon.log": b"fixture daemon output\n",
            "comparison.json": _json_bytes(comparison),
            "bridge-manifest.json": _json_bytes({"closed_cleanly": bool(complete), "failure": None if complete else "fixture incomplete"}),
            "wal/raw.wal.ndjson": wal_bytes,
        }
        file_hashes = {name: _sha256(data) for name, data in files.items()}
        manifest = {
            "schema_version": "l15-manifest.v1",
            "run_id": f"{case_id}-{run_index}",
            "case_id": case_id,
            "created_at": "2026-11-27T00:00:00+00:00",
            "mode": "bridge",
            "topology": "dut_local_process_daemon_native",
            "transport": {"mode": "virtual_pty", "port_id": "fixture-port", "daemon_side": None, "bridge_side": None},
            "artifacts": {
                "cli": {"path": "fixture-cli", "sha256": identity.get("cli", {}).get("sha256"),
                        "version_stdout": "serialwrap fixture"},
                "daemon": {
                    "argv": ["fixture-daemon"],
                    "implementation": identity.get("daemon", {}).get("implementation"),
                    "sha256": identity.get("daemon", {}).get("sha256"),
                    "package_tree_sha256": identity.get("daemon", {}).get("tree_sha256"),
                    "repo_tree_sha256": "9" * 64 if side.lower() == "a" else None,
                },
                "release_manifest_sha256": identity.get("release_manifest_sha256"),
                "deployed_content": {
                    "start": _deployed_content(identity),
                    "end": _deployed_content(identity),
                    "daemon_live": [],
                },
            },
            "tool": {"name": "fixture", "version": "1", "tree_sha256": "6" * 64, "approved": True},
            "dut": {"mode": "local-process", "image": None, "image_id": None,
                    "target_sha256": "7" * 64, "scenario_sha256": None, "seed": None},
            "repo_git_revision": None,
            "repo_dirty": False,
            "contract_revision": None,
            "contract_dirty": False,
            "cases_yaml_sha256": None,
            "case": {"source_path": "fixture", "sha256": "8" * 64, "revision": None},
            "platform": {"os": "fixture", "release": "fixture", "version": "fixture", "machine": "fixture",
                         "python": "3", "evidence_insufficient": []},
            "bridge_manifest_sha256": file_hashes["bridge-manifest.json"],
            "evidence": {
                "wire": {"path": "wire.ndjson", "sha256": file_hashes["wire.ndjson"]},
                "control": {"path": "control.ndjson", "sha256": file_hashes["control.ndjson"]},
                "correlation": {"path": "correlation.ndjson", "sha256": file_hashes["correlation.ndjson"]},
                "dut_stderr": {"path": "dut.stderr.log", "sha256": file_hashes["dut.stderr.log"]},
                "cli_log": {"path": "cli.ndjson", "sha256": file_hashes["cli.ndjson"]},
                "wal_dir": "wal",
            },
        }
        files["manifest.json"] = _json_bytes(manifest)
        for rel, data in files.items():
            if rel not in missing_set and not any(rel.startswith(m.rstrip("/") + "/") for m in missing_set):
                _write_bytes(run_dir / rel, data)
        return run_dir

    run_dir.mkdir(parents=True, exist_ok=True)
    release_manifest = _release_manifest_bytes(identity)
    if "release_manifest_sha256" in identity:
        identity["release_manifest_sha256"] = _sha256(release_manifest)
    source_specs = _source_specs(
        runner,
        complete=complete,
        verdict=verdict,
        invariants=normalized_invariants,
        release_manifest=release_manifest,
        runner_exit_code=runner_exit_code,
    )
    if runner in {"contract-blackbox", "windows-native"}:
        l1 = json.loads(source_specs["source/l1-results.json"].decode("utf-8"))
        l1["collected"] = [case_id]
        l1["results"][0]["nodeid"] = case_id
        source_specs["source/l1-results.json"] = _json_bytes(l1)
    else:
        l1 = json.loads(source_specs["source/l1-results.json"].decode("utf-8"))
        l1["collected"] = [f"cargo:fixture::{case_id}::case"]
        l1["results"][0]["nodeid"] = f"cargo:fixture::{case_id}::case"
        source_specs["source/l1-results.json"] = _json_bytes(l1)
    raw_call_rows = [
        {
            "call_index": index,
            **({"argv": row["argv"]} if "argv" in row else {}),
            **({"rc": row["rc"]} if "rc" in row else {}),
            **({"stdout": row["stdout"]} if "stdout" in row else {}),
        }
        for index, row in enumerate(call_rows)
    ]
    source_specs["source/cli-observations.ndjson"] = b"".join(_json_bytes(row) for row in raw_call_rows)
    for rel, data in source_specs.items():
        if rel not in missing_set:
            _write_bytes(run_dir / rel, data)

    source_rows = _source_rows(run_dir, source_specs)
    envelope_observations = [
        {
            "argv": list(row.get("argv", ())),
            **({"rc": row["rc"]} if "rc" in row else {}),
            **({"stdout": row["stdout"]} if "stdout" in row else {}),
            "source_ref": f"{case_id}/{runner}/{run_index}/source/cli-observations.ndjson#L{index + 1}",
        }
        for index, row in enumerate(call_rows)
    ] if observations is None else [dict(row) for row in observations]
    envelope = {
        "schema_version": OBSERVATION_SCHEMA,
        "runner": runner,
        "case_id": case_id,
        "run_index": run_index,
        "completion": {
            "status": "completed" if complete else "incomplete",
            "finished": bool(complete),
            "exit_code": runner_exit_code if runner_exit_code is not None else (1 if (not complete or verdict == "FAIL") else 0),
        },
        "artifacts": identity,
        "verdict": verdict,
        "reason": reason,
        "invariants": normalized_invariants,
        "cli_observations": envelope_observations,
        "source_files": source_rows,
    }
    _write_json(run_dir / "runner-observation.json", envelope)
    return run_dir


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"預期 JSON 物件：{path}")
    return value


def write_json(path: Path, value: Any) -> Path:
    """供測試改寫純資料報告；仍以 write_bytes 保留固定換行。"""
    return _write_json(Path(path), value)


def source_path(report_dir: Path, case_id: str, runner: str, run_index: int, rel: str) -> Path:
    """由側 report-dir 與相對 run 檔定位原始來源。"""
    return Path(report_dir) / case_id / runner / str(run_index) / Path(rel)


def source_sha256(path: Path) -> str:
    """使用 T2 共用替身計算原始檔 SHA-256。"""
    return sha256_file(Path(path))


__all__ = [
    "CASE_ID",
    "CLI_OBSERVATIONS_SCHEMA",
    "OBSERVATION_SCHEMA",
    "RUNNERS",
    "read_json",
    "source_path",
    "source_sha256",
    "write_json",
    "write_run",
]
