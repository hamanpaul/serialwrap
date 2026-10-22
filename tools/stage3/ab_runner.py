"""依序執行凍結 manifest 指定的 Python 基線與 Rust 候選 A/B。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import ab_diff, ab_manifest, ab_verdict, dut_state, jsonio, proc, release_check, suites
from .ab_manifest import AbManifest, AbSide
from .proc import Runner, Spawner, run_cmd
from .reason_codes import HarnessError
from .release_check import PreStopSnapshot, ReleaseProbe, ReleaseResult
from .suites import SuiteRun


VERDICT_PRECEDENCE: tuple[str, ...] = ("FAIL", "BASELINE_INVALID", "INFRA_ERROR", "ABORTED", "PASS")


@dataclass(frozen=True)
class SideResult:
    label: str
    role: str
    round: int
    suite: SuiteRun | None
    identity_violations: tuple[str, ...]
    report_dir: Path
    clean_violations: tuple[str, ...]
    pre_stop: PreStopSnapshot | None
    release: ReleaseResult | None
    cleanup_violations: tuple[str, ...]
    started_at: str
    finished_at: str
    abort_code: str | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundResult:
    round: int
    order: tuple[str, str]
    diff: dict | None
    verdict: str
    reason_code: str | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class AbRunResult:
    verdict: str
    reason_code: str | None
    manifest_sha256: str
    sides: tuple[SideResult, ...]
    rounds: tuple[RoundResult, ...]
    report_path: Path


def run_ab(
    manifest_path: Path,
    *,
    out_dir: Path,
    repo_root: Path,
    run: Runner = run_cmd,
    spawn: Spawner = proc.spawn,
    probe: ReleaseProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = jsonio.utc_now_iso,
) -> AbRunResult:
    """依凍結設定執行 A/B；所有提前結束路徑都保留一份報告。"""
    manifest = ab_manifest.load_manifest(Path(manifest_path))
    output = Path(out_dir).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise HarnessError("HARNESS_MANIFEST_INVALID", f"輸出目錄已存在：{output}") from exc

    report_path = output / "ab-report.json"
    root = Path(repo_root).resolve()
    started_at = now()
    selected_probe = probe if probe is not None else release_check.default_probe()
    initial_reason: str | None = None
    initial_verdict = "INFRA_ERROR"
    initial_notes: list[str] = []
    trees: dict[str, dict[str, str]] = {}
    side_results: list[SideResult] = []
    round_results: list[RoundResult] = []
    pending_interrupt: KeyboardInterrupt | None = None

    try:
        try:
            violations = ab_manifest.verify_frozen(manifest, repo_root=root, run=run)
        except HarnessError as exc:
            violations = [exc.detail or str(exc)]
        except Exception as exc:  # noqa: BLE001 - 無法驗證凍結輸入時採 fail-closed
            violations = [f"verify_frozen failed: {type(exc).__name__}: {exc}"]
        if violations:
            initial_reason = _initial_reason(violations[0])
            initial_notes.extend(violations)
        else:
            for side in manifest.sides:
                try:
                    trees[side.label] = {
                        "bundle_tree_sha256": ab_manifest.bundle_tree_sha256(side.bundle_dir),
                        "runtime_tree_sha256": ab_manifest.runtime_tree_sha256(side.bundle_dir),
                    }
                except HarnessError as exc:
                    initial_reason = "HARNESS_MANIFEST_DRIFT"
                    initial_notes.append(f"{side.label}: {exc}")
                    break
                except OSError as exc:
                    initial_reason = "HARNESS_MANIFEST_DRIFT"
                    initial_notes.append(f"{side.label}: bundle tree unavailable: {exc}")
                    break

        if initial_reason is None and manifest.layer == "l3":
            initial_reason, notes = _check_od09(manifest, root, now)
            initial_notes.extend(notes)

        if initial_reason is None:
            orders = [("A", "B")]
            if manifest.reverify.enabled:
                orders.append(("B", "A"))
            candidate_registered = False
            stop_all = False

            for round_number, order in enumerate(orders, start=1):
                this_round: list[SideResult] = []
                round_abort: str | None = None
                for position, label in enumerate(order):
                    if manifest.layer == "l3" and label == "B" and not candidate_registered:
                        try:
                            _register_candidate(manifest, root, now)
                            candidate_registered = True
                        except Exception as exc:  # noqa: BLE001 - 候選未登記不得啟動
                            round_abort = _od09_error_code(exc)
                            initial_notes.append(f"candidate start registration failed: {type(exc).__name__}: {exc}")
                            placeholder = _not_run_side(manifest, label, round_number, output, now())
                            side_results.append(placeholder)
                            this_round.append(placeholder)
                            for skipped in order[position + 1:]:
                                placeholder = _not_run_side(manifest, skipped, round_number, output, now())
                                side_results.append(placeholder)
                                this_round.append(placeholder)
                            break

                    result, interrupted = _run_side(
                        manifest,
                        label,
                        round_number,
                        output,
                        root,
                        trees,
                        run=run,
                        spawn=spawn,
                        probe=selected_probe,
                        clock=clock,
                        sleep=sleep,
                        now=now,
                    )
                    side_results.append(result)
                    this_round.append(result)
                    if interrupted is not None:
                        pending_interrupt = interrupted
                    if result.abort_code is not None:
                        round_abort = result.abort_code
                        for skipped in order[position + 1:]:
                            placeholder = _not_run_side(manifest, skipped, round_number, output, now())
                            side_results.append(placeholder)
                            this_round.append(placeholder)
                        break

                if round_abort is not None:
                    failed = _aborted_product_failure(this_round, manifest.layer)
                    verdict = "FAIL" if failed else (
                        "ABORTED" if round_abort == "HARNESS_ABORTED" else "INFRA_ERROR"
                    )
                    notes = [f"{item.label}: aborted with {item.abort_code}" for item in this_round
                             if item.abort_code]
                    notes.extend(f"{item.label}: {note}" for item in this_round for note in item.notes)
                    if failed:
                        notes.append("aborted after product failure")
                    round_results.append(RoundResult(
                        round_number, order, None, verdict, round_abort, tuple(notes)
                    ))
                    stop_all = True
                else:
                    try:
                        diff, verdict, reason_code, notes = _compare_round(
                            manifest, this_round, root, now=now
                        )
                    except HarnessError as exc:
                        diff = None
                        product_failed = _aborted_product_failure(this_round, manifest.layer)
                        verdict = "FAIL" if product_failed else "INFRA_ERROR"
                        reason_code = "HARNESS_COMPARATOR_ERROR"
                        notes = [str(exc)]
                        if product_failed:
                            notes.append("comparison failed after product failure")
                    except Exception as exc:  # noqa: BLE001 - 比較不能產生可靠判定
                        diff = None
                        product_failed = _aborted_product_failure(this_round, manifest.layer)
                        verdict = "FAIL" if product_failed else "INFRA_ERROR"
                        reason_code = "HARNESS_COMPARATOR_ERROR"
                        notes = [f"comparator failed: {type(exc).__name__}: {exc}"]
                        if product_failed:
                            notes.append("comparison failed after product failure")
                    round_results.append(RoundResult(
                        round_number, order, diff, verdict, reason_code, tuple(notes)
                    ))
                if stop_all:
                    break

    except KeyboardInterrupt as exc:
        pending_interrupt = exc
        if not any(item.verdict == "FAIL" for item in round_results):
            round_results.append(RoundResult(
                len(round_results) + 1, ("A", "B"), None,
                "ABORTED", "HARNESS_ABORTED", ("執行期間收到 KeyboardInterrupt",)
            ))
    except HarnessError as exc:
        initial_reason = exc.code
        initial_notes.append(exc.detail or str(exc))
    except Exception as exc:  # noqa: BLE001 - 意外 harness 錯誤也必須留下報告
        initial_reason = "HARNESS_SUITE_INFRA_ERROR"
        initial_notes.append(f"A/B 執行器錯誤：{type(exc).__name__}: {exc}")

    verdict, reason_code = _final_result(round_results, initial_verdict, initial_reason)
    if pending_interrupt is not None and verdict not in {"FAIL"}:
        verdict, reason_code = "ABORTED", "HARNESS_ABORTED"

    finished_at = now()
    report = _build_report(
        manifest,
        root,
        output,
        trees,
        side_results,
        round_results,
        verdict=verdict,
        reason_code=reason_code,
        started_at=started_at,
        finished_at=finished_at,
        run=run,
        initial_notes=initial_notes,
    )
    jsonio.atomic_write_text(report_path, jsonio.dumps_report(report))

    result = AbRunResult(
        verdict=verdict,
        reason_code=reason_code,
        manifest_sha256=manifest.source_sha256,
        sides=tuple(side_results),
        rounds=tuple(round_results),
        report_path=report_path,
    )
    if pending_interrupt is not None:
        raise pending_interrupt
    return result


def _check_od09(
    manifest: AbManifest,
    repo_root: Path,
    now: Callable[[], str],
) -> tuple[str | None, list[str]]:
    """確認 L3 門檻已於 A 側開始前凍結。"""
    from realhw.acceptance import od09_freeze

    if manifest.acceptance is None:
        return "HARNESS_OD09_NOT_FROZEN", ["L3 manifest 缺少 acceptance"]
    acceptance = _input_path(repo_root, manifest.acceptance.path)
    try:
        violations = od09_freeze.check_frozen_before(acceptance, now())
    except Exception as exc:  # noqa: BLE001 - 無法確認凍結時序即拒絕執行
        return "HARNESS_OD09_NOT_FROZEN", [f"OD-09 凍結檢查失敗：{type(exc).__name__}: {exc}"]
    if violations:
        return "HARNESS_OD09_NOT_FROZEN", list(violations)
    return None, []


def _register_candidate(manifest: AbManifest, repo_root: Path, now: Callable[[], str]) -> None:
    """在第一個 L3 候選側啟動前登記；重驗輪不重複登記。"""
    from realhw.acceptance import od09_freeze

    if manifest.acceptance is None:
        raise HarnessError("HARNESS_OD09_NOT_FROZEN", "L3 manifest 缺少 acceptance")
    try:
        od09_freeze.register_candidate_start(
            _input_path(repo_root, manifest.acceptance.path),
            item_id=f"S3-L3-AB-48H-{manifest.platform.upper()}",
            platform=manifest.platform,
            run_id=manifest.run_id,
            started_at=now(),
        )
    except od09_freeze.Od09Error as exc:
        raise HarnessError("HARNESS_OD09_NOT_FROZEN", str(exc)) from exc
    except ValueError as exc:
        raise HarnessError("HARNESS_MANIFEST_INVALID", str(exc)) from exc


def _od09_error_code(exc: Exception) -> str:
    if isinstance(exc, HarnessError):
        return exc.code
    return "HARNESS_OD09_NOT_FROZEN"


def _run_side(
    manifest: AbManifest,
    label: str,
    round_number: int,
    out_dir: Path,
    repo_root: Path,
    trees: Mapping[str, Mapping[str, str]],
    *,
    run: Runner,
    spawn: Spawner,
    probe: ReleaseProbe,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    now: Callable[[], str],
) -> tuple[SideResult, KeyboardInterrupt | None]:
    side = _side(manifest, label)
    started_at = now()
    report_dir = out_dir / f"r{round_number}-{label}" / "report"
    suite_run: SuiteRun | None = None
    identity_violations: list[str] = []
    clean_violations: list[str] = []
    cleanup_violations: list[str] = []
    pre_stop: PreStopSnapshot | None = None
    release: ReleaseResult | None = None
    notes: list[str] = []
    abort_code: str | None = None
    interrupted: KeyboardInterrupt | None = None
    instance: dut_state.Instance | None = None

    def abort(code: str, note: str | None = None) -> None:
        nonlocal abort_code
        if abort_code is None:
            abort_code = code
        elif abort_code != code:
            notes.append(f"additional abort condition: {code}")
        if note:
            notes.append(note)

    def defer_interrupt(exc: KeyboardInterrupt) -> None:
        nonlocal interrupted
        if interrupted is None:
            interrupted = exc
        abort("HARNESS_ABORTED", "收尾期間收到 KeyboardInterrupt")

    try:
        try:
            violations = ab_manifest.verify_frozen(manifest, repo_root=repo_root, run=run)
        except Exception as exc:  # noqa: BLE001 - 驗證例外按凍結漂移處理
            violations = [f"verify_frozen failed: {type(exc).__name__}: {exc}"]
        if violations:
            abort("HARNESS_MANIFEST_DRIFT", "; ".join(violations))
        else:
            try:
                current = {
                    "bundle_tree_sha256": ab_manifest.bundle_tree_sha256(side.bundle_dir),
                    "runtime_tree_sha256": ab_manifest.runtime_tree_sha256(side.bundle_dir),
                }
            except Exception as exc:  # noqa: BLE001 - 非一般檔案或缺少 bundle 都是 artifact 漂移
                current = {}
                abort("HARNESS_MANIFEST_DRIFT", f"bundle tree unavailable: {type(exc).__name__}: {exc}")
            if current and current != trees.get(label):
                abort("HARNESS_MANIFEST_DRIFT", "bundle tree changed before side")

        if abort_code is None:
            try:
                instance = dut_state.fresh_instance(
                    manifest,
                    side,
                    round=round_number,
                    base_dir=out_dir,
                    repo_root=repo_root,
                )
                report_dir = instance.report_dir
            except HarnessError as exc:
                abort(exc.code, exc.detail)
            except Exception as exc:  # noqa: BLE001 - instance setup failure is infrastructure
                abort("HARNESS_DUT_NOT_CLEAN", f"instance setup failed: {type(exc).__name__}: {exc}")

        if instance is not None and manifest.layer in {"l2", "l3"} and abort_code is None:
            try:
                argv = ab_manifest.render_argv(
                    side.activate_argv,
                    _template_values(manifest, side, instance, out_dir, repo_root),
                )
                activated = run(argv, env=instance.env, timeout=600, cwd=repo_root)
                if int(getattr(activated, "rc", -1)) != 0 or bool(getattr(activated, "timed_out", False)):
                    abort("HARNESS_SUITE_INFRA_ERROR", "side activation failed")
            except HarnessError as exc:
                abort(exc.code, exc.detail)
            except Exception as exc:  # noqa: BLE001 - activation failure still enters release cleanup
                abort("HARNESS_SUITE_INFRA_ERROR", f"side activation failed: {type(exc).__name__}: {exc}")

        if instance is not None and abort_code is None:
            try:
                clean_violations = dut_state.verify_clean(
                    manifest,
                    instance,
                    side,
                    run=run,
                    repo_root=repo_root,
                    out_dir=out_dir,
                )
            except HarnessError as exc:
                clean_violations = [exc.detail or str(exc)]
                abort(exc.code, exc.detail)
            except Exception as exc:  # noqa: BLE001 - 無法證明 DUT 乾淨即拒絕執行 suite
                clean_violations = [f"clean check failed: {type(exc).__name__}: {exc}"]
                abort("HARNESS_DUT_NOT_CLEAN", clean_violations[-1])
            if clean_violations and abort_code is None:
                abort("HARNESS_DUT_NOT_CLEAN", "; ".join(clean_violations))

        if instance is not None and abort_code is None:
            suite_run = suites.run_side(
                manifest,
                side,
                instance,
                run=run,
                spawn=spawn,
                now=now,
                probe=probe,
                repo_root=repo_root,
            )
            notes.extend(suite_run.notes)
            if suite_run.status == "INFRA_ERROR":
                abort(_suite_error_code(suite_run), "; ".join(suite_run.notes))
            else:
                identity_violations = suites.verify_run_identity(
                    manifest,
                    side,
                    suite_run,
                    expected_trees=trees[label],
                )
                if identity_violations:
                    abort("HARNESS_ARTIFACT_MISMATCH", "; ".join(identity_violations))
    except KeyboardInterrupt as exc:
        defer_interrupt(exc)
        abort("HARNESS_ABORTED", "執行期間收到 KeyboardInterrupt")
    except HarnessError as exc:
        abort(exc.code, exc.detail)
    except Exception as exc:  # noqa: BLE001 - 未預期錯誤不得略過收尾
        abort("HARNESS_SUITE_INFRA_ERROR", f"side execution failed: {type(exc).__name__}: {exc}")
    finally:
        if instance is not None:
            if manifest.layer == "l1.5":
                endpoints_path = instance.report_dir / suites.REPORT_ENDPOINTS
                if not endpoints_path.is_file():
                    notes.append("endpoints.json missing")
                for attempt in range(2):
                    try:
                        pre_stop = release_check.snapshot_from_endpoints(
                            endpoints_path,
                            manifest.release,
                            match_paths=_match_paths(side),
                            clock=clock,
                        )
                    except KeyboardInterrupt as exc:
                        defer_interrupt(exc)
                        if attempt == 1:
                            abort("HARNESS_RELEASE_UNCONFIRMED", "release snapshot interrupted")
                    except Exception as exc:  # noqa: BLE001 - 快照不足以確認釋放
                        abort("HARNESS_RELEASE_UNCONFIRMED",
                              f"release snapshot failed: {type(exc).__name__}: {exc}")
                        break
                    else:
                        break
            else:
                for attempt in range(2):
                    try:
                        pre_stop = release_check.capture_pre_stop(
                            side.cli_exe,
                            instance.env,
                            manifest.release,
                            daemon_exe=side.daemon_exe,
                            entry_scripts=_entry_scripts(side),
                            endpoint=manifest.endpoint,
                            run=run,
                            probe=probe,
                            clock=clock,
                        )
                    except KeyboardInterrupt as exc:
                        defer_interrupt(exc)
                        if attempt == 1:
                            abort("HARNESS_RELEASE_UNCONFIRMED", "release snapshot interrupted")
                    except Exception as exc:  # noqa: BLE001 - 即使快照失敗仍嘗試停止候選
                        abort("HARNESS_RELEASE_UNCONFIRMED",
                              f"release snapshot failed: {type(exc).__name__}: {exc}")
                        break
                    else:
                        break

                deactivate: list[str] | None = None
                try:
                    deactivate = ab_manifest.render_argv(
                        side.deactivate_argv,
                        _template_values(manifest, side, instance, out_dir, repo_root),
                    )
                except HarnessError as exc:
                    abort(exc.code, exc.detail)
                except Exception as exc:  # noqa: BLE001 - 停止命令無法建立
                    abort("HARNESS_RELEASE_UNCONFIRMED",
                          f"side deactivation could not be prepared: {type(exc).__name__}: {exc}")
                if deactivate is not None:
                    try:
                        stopped = run(deactivate, env=instance.env, timeout=600, cwd=repo_root)
                        if (int(getattr(stopped, "rc", -1)) != 0
                                or bool(getattr(stopped, "timed_out", False))):
                            abort("HARNESS_RELEASE_UNCONFIRMED", "side deactivation failed")
                    except KeyboardInterrupt as exc:
                        defer_interrupt(exc)
                        abort("HARNESS_RELEASE_UNCONFIRMED",
                              "side deactivation interrupted; command not retried")
                    except Exception as exc:  # noqa: BLE001 - 停止失敗不得啟動下一側
                        abort("HARNESS_RELEASE_UNCONFIRMED",
                              f"side deactivation failed: {type(exc).__name__}: {exc}")

            if pre_stop is not None:
                for attempt in range(2):
                    try:
                        release = release_check.verify_released(
                            pre_stop,
                            manifest.release,
                            probe=probe,
                            clock=clock,
                            sleep=sleep,
                        )
                    except KeyboardInterrupt as exc:
                        defer_interrupt(exc)
                        if attempt == 1:
                            release = _unknown_release("release verification interrupted")
                            abort("HARNESS_RELEASE_UNCONFIRMED", release.checks[0].detail)
                    except Exception as exc:  # noqa: BLE001 - 探測例外不得視為 released
                        release = _unknown_release(
                            f"release verification failed: {type(exc).__name__}: {exc}"
                        )
                        abort("HARNESS_RELEASE_UNCONFIRMED", release.checks[0].detail)
                        break
                    else:
                        break
            else:
                release = _unknown_release("release snapshot unavailable")

            for attempt in range(2):
                try:
                    cleanup_violations = dut_state.cleanup_instance(instance)
                except KeyboardInterrupt as exc:
                    defer_interrupt(exc)
                    if attempt == 1:
                        cleanup_violations = ["instance cleanup interrupted"]
                        abort("HARNESS_RELEASE_UNCONFIRMED", cleanup_violations[-1])
                except Exception as exc:  # noqa: BLE001 - 清理不完整即阻止下一側
                    cleanup_violations = [f"instance cleanup failed: {type(exc).__name__}: {exc}"]
                    break
                else:
                    break

            if release is None or not release.released or cleanup_violations:
                abort("HARNESS_RELEASE_UNCONFIRMED", "release unconfirmed")

            after: dict[str, str] = {}
            for attempt in range(2):
                try:
                    after = {
                        "bundle_tree_sha256": ab_manifest.bundle_tree_sha256(side.bundle_dir),
                        "runtime_tree_sha256": ab_manifest.runtime_tree_sha256(side.bundle_dir),
                    }
                except KeyboardInterrupt as exc:
                    defer_interrupt(exc)
                    if attempt == 1:
                        notes.append("bundle tree unavailable after side: interrupted")
                        abort("HARNESS_MANIFEST_DRIFT", "post-release bundle tree check interrupted")
                except Exception as exc:  # noqa: BLE001 - 無法重算內容指紋即視為漂移
                    notes.append(f"bundle tree unavailable after side: {type(exc).__name__}: {exc}")
                    abort("HARNESS_MANIFEST_DRIFT", "post-release bundle tree check failed")
                    break
                else:
                    break
            if after and after != trees.get(label):
                notes.append("bundle tree changed during side")
                abort("HARNESS_MANIFEST_DRIFT")

    result = SideResult(
        label=label,
        role=side.role,
        round=round_number,
        suite=suite_run,
        identity_violations=tuple(identity_violations),
        report_dir=report_dir,
        clean_violations=tuple(clean_violations),
        pre_stop=pre_stop,
        release=release,
        cleanup_violations=tuple(cleanup_violations),
        started_at=started_at,
        finished_at=now(),
        abort_code=abort_code,
        notes=tuple(notes),
    )
    return result, interrupted


def _compare_round(
    manifest: AbManifest,
    sides: list[SideResult],
    repo_root: Path,
    *,
    now: Callable[[], str],
) -> tuple[dict | None, str, str | None, list[str]]:
    by_label = {item.label: item for item in sides if item.suite is not None}
    a = by_label.get("A")
    b = by_label.get("B")
    if a is None or b is None or a.suite is None or b.suite is None:
        return None, "INFRA_ERROR", "HARNESS_SUITE_INFRA_ERROR", ["必要側未產生 suite 結果"]

    required_case_ids, cases = ab_manifest.required_cases(manifest, repo_root=repo_root)
    changes_path = _input_path(repo_root, manifest.allowed_differences.path)
    registered = ab_verdict.load_registered_changes(changes_path)
    case_change_attribution = ab_verdict.case_changes(changes_path)
    registered_case_changes = ab_verdict.case_changes(changes_path, scope="both")

    if manifest.layer == "l1.5":
        rules_path = repo_root / "contracts" / "ab_normalization.yaml"
        diff = ab_diff.diff_l15(
            a.report_dir,
            b.report_dir,
            registered=registered,
            case_changes=case_change_attribution,
            required_case_ids=required_case_ids,
            rules_path=rules_path,
        )
    elif manifest.layer == "l2":
        diff = ab_verdict.compare_l2_reports(
            _read_json_object(a.report_dir / "testpilot" / "report.json"),
            _read_json_object(b.report_dir / "testpilot" / "report.json"),
            required_case_ids=required_case_ids,
            cases=cases,
            registered_case_changes=registered_case_changes,
            now=now(),
        )
    else:
        diff = ab_verdict.compare_l3(
            a.report_dir / "release-gate.json",
            b.report_dir / "release-gate.json",
        )
    verdict, reason_code, notes = ab_verdict.combine(
        manifest.layer,
        a_status=a.suite.status,
        b_status=b.suite.status,
        diff=diff,
        registered=registered,
    )
    return diff, verdict, reason_code, notes


def _aborted_product_failure(sides: list[SideResult], layer: str) -> bool:
    for side in sides:
        if side.suite is not None and side.role == "candidate" and side.suite.status == "FAIL":
            return True
        try:
            if layer == "l1.5" and ab_diff.safety_tokens(side.report_dir, side.label):
                return True
            if layer == "l3" and _l3_safety_failure(side.report_dir / "release-gate.json"):
                return True
        except Exception:
            continue
    return False


def _l3_safety_failure(path: Path) -> bool:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    checks = doc.get("checks") if isinstance(doc, dict) else None
    return isinstance(checks, list) and any(
        isinstance(check, Mapping)
        and check.get("group") == "safety"
        and check.get("result") == "FAIL"
        for check in checks
    )


def _initial_reason(violation: str) -> str:
    if violation.startswith(("required case missing:", "unknown case:", "invariant not covered:")):
        return "HARNESS_CASE_SET_INCOMPLETE"
    if violation.startswith(("destructive case without authorization:", "authorization board not in dut:",
                             "windows com0com instance without test_device_allowlist")):
        return "HARNESS_AUTHORIZATION_MISSING"
    return "HARNESS_MANIFEST_DRIFT"


def _suite_error_code(suite_run: SuiteRun) -> str:
    for note in suite_run.notes:
        for code in ("HARNESS_FAILURE_RETENTION_UNVERIFIED", "HARNESS_OD09_NOT_FROZEN"):
            if note.startswith(code):
                return code
    return "HARNESS_SUITE_INFRA_ERROR"


def _side(manifest: AbManifest, label: str) -> AbSide:
    return manifest.sides[0] if label == "A" else manifest.sides[1]


def _input_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _template_values(
    manifest: AbManifest,
    side: AbSide,
    instance: dut_state.Instance,
    out_dir: Path,
    repo_root: Path,
) -> dict[str, str]:
    return {
        "cli_exe": str(side.cli_exe),
        "daemon_exe": str(side.daemon_exe),
        "bundle_dir": str(side.bundle_dir),
        "report_dir": str(instance.report_dir),
        "instance_dir": str(instance.root),
        "seed": str(manifest.seed),
        "side": side.label,
        "round": str(instance.round),
        "endpoint": manifest.endpoint or "",
        "out_dir": str(out_dir),
        "repo_root": str(repo_root),
    }


def _entry_scripts(side: AbSide) -> tuple[str, ...]:
    components = side.manifest_doc.get("components")
    daemon = components.get("daemon") if isinstance(components, Mapping) else None
    runtime = daemon.get("runtime") if isinstance(daemon, Mapping) else None
    path = runtime.get("path") if isinstance(runtime, Mapping) else None
    if not isinstance(path, str) or not path:
        return ()
    target = Path(path)
    if not target.is_absolute():
        target = side.bundle_dir / target
    return (str(target.resolve()),)


def _match_paths(side: AbSide) -> tuple[str, ...]:
    return (str(side.daemon_exe.resolve()), *_entry_scripts(side))


def _unknown_release(detail: str) -> ReleaseResult:
    check = release_check.CheckOutcome("release", "", "unknown", detail)
    return ReleaseResult(False, (check,), 0.0)


def _not_run_side(
    manifest: AbManifest,
    label: str,
    round_number: int,
    out_dir: Path,
    timestamp: str,
) -> SideResult:
    side = _side(manifest, label)
    return SideResult(
        label=label,
        role=side.role,
        round=round_number,
        suite=None,
        identity_violations=(),
        report_dir=out_dir / f"r{round_number}-{label}" / "report",
        clean_violations=(),
        pre_stop=None,
        release=None,
        cleanup_violations=(),
        started_at=timestamp,
        finished_at=timestamp,
        abort_code=None,
    )


def _final_result(
    rounds: list[RoundResult],
    default_verdict: str,
    default_reason: str | None,
) -> tuple[str, str | None]:
    if not rounds:
        return default_verdict, default_reason
    rank = {value: index for index, value in enumerate(VERDICT_PRECEDENCE)}
    candidates = [(item.verdict, item.reason_code) for item in rounds]
    if default_reason is not None:
        candidates.append((default_verdict, default_reason))
    return min(candidates, key=lambda item: rank.get(item[0], len(rank)))


def _build_report(
    manifest: AbManifest,
    repo_root: Path,
    out_dir: Path,
    trees: Mapping[str, Mapping[str, str]],
    side_results: list[SideResult],
    rounds: list[RoundResult],
    *,
    verdict: str,
    reason_code: str | None,
    started_at: str,
    finished_at: str,
    run: Runner,
    initial_notes: list[str],
) -> dict[str, Any]:
    # evidence_ingest 的契約以一組 A/B 身分為索引；複驗輪的逐次資料另完整留在 side_runs。
    logical_sides = []
    for label in ("A", "B"):
        executions = [item for item in side_results if item.label == label]
        primary = next((item for item in executions if item.round == 1), None)
        if primary is None:
            primary = _not_run_side(manifest, label, 1, out_dir, started_at)
        record = _side_report(manifest, primary, trees.get(label, {}))
        record["runs"] = [_side_report(manifest, item, trees.get(label, {})) for item in executions]
        logical_sides.append(record)

    tools = []
    for frozen in manifest.tools:
        item: dict[str, Any] = {"path": frozen.path}
        if isinstance(frozen, ab_manifest.FrozenTree):
            item["tree_sha256"] = frozen.tree_sha256
        else:
            item["sha256"] = frozen.sha256
        tools.append(item)

    revision = _tool_revision(run, repo_root)
    try:
        stage3_tree = ab_manifest._tree_sha256(Path(__file__).resolve().parent)
    except Exception:
        stage3_tree = None

    host_name = socket.gethostname()
    return {
        "ab_manifest_sha256": manifest.source_sha256,
        "run_id": manifest.run_id,
        "layer": manifest.layer,
        "platform": manifest.platform,
        "stage": manifest.stage,
        "contract_revision": manifest.contract_revision,
        "case_list_sha256": manifest.case_list.sha256,
        "case_lock_sha256": manifest.case_lock.sha256,
        "required_case_set": manifest.required_case_set,
        "case_ids": list(manifest.case_ids),
        "tools": tools,
        "tool_revision": revision,
        "tools_stage3_tree_sha256": stage3_tree,
        "dut": {
            "kind": manifest.dut.kind,
            "transport": manifest.dut.transport,
            "com_pair": _jsonable(manifest.dut.com_pair),
            "image": manifest.dut.image,
            "image_digest": manifest.dut.image_digest,
            "boards": list(manifest.dut.boards),
        },
        "authorization": _authorization_report(manifest),
        "endpoint": manifest.endpoint,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "hostname_sha256_16": hashlib.sha256(host_name.encode("utf-8")).hexdigest()[:16],
        },
        "sides": logical_sides,
        "side_runs": [_side_report(manifest, item, trees.get(item.label, {})) for item in side_results],
        "rounds": [_round_report(item) for item in rounds],
        "verdict": verdict,
        "reason_code": reason_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "notes": list(initial_notes),
    }


def _side_report(
    manifest: AbManifest,
    result: SideResult,
    trees: Mapping[str, str],
) -> dict[str, Any]:
    side = _side(manifest, result.label)
    suite_doc = None
    daemon_status_version = None
    evidence_layer = None
    if result.suite is not None:
        identity = _jsonable(result.suite.identity)
        suite_doc = {"status": result.suite.status, "rc": result.suite.rc, "identity": identity}
        if isinstance(result.suite.identity, Mapping):
            daemon = result.suite.identity.get("daemon")
            if isinstance(daemon, Mapping):
                daemon_status_version = daemon.get("version")
            daemon_status_version = result.suite.identity.get("daemon_version") or daemon_status_version
            evidence_layer = result.suite.identity.get("evidence_layer")

    return {
        "label": result.label,
        "role": result.role,
        "round": result.round,
        "status": ("not_run" if result.suite is None and result.abort_code is None
                   else result.suite.status if result.suite is not None else "aborted"),
        "release_manifest_sha256": side.release_manifest.sha256,
        "runtime_digest": ab_manifest.runtime_digest(side.manifest_doc),
        "bundle_tree_sha256": trees.get("bundle_tree_sha256"),
        "runtime_tree_sha256": trees.get("runtime_tree_sha256"),
        "cli_exe": str(side.cli_exe.resolve()),
        "cli_sha256": _file_digest(side.cli_exe),
        "daemon_exe": str(side.daemon_exe.resolve()),
        "daemon_sha256": _file_digest(side.daemon_exe),
        "bundle_version": side.manifest_doc.get("bundle_version"),
        "daemon_status_version": daemon_status_version,
        "suite": suite_doc,
        "evidence_layer": evidence_layer,
        "identity_violations": list(result.identity_violations),
        "clean_violations": list(result.clean_violations),
        "pre_stop": _pre_stop_report(result.pre_stop),
        "release": _release_report(result.release),
        "cleanup_violations": list(result.cleanup_violations),
        "notes": list(result.notes),
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "abort_code": result.abort_code,
    }


def _release_report(result: ReleaseResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "released": result.released,
        "checks": [asdict(item) for item in result.checks],
        "elapsed_s": result.elapsed_s,
    }


def _pre_stop_report(snapshot: PreStopSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "daemon_pid": snapshot.daemon_pid,
        "descendant_pids": list(snapshot.descendant_pids),
        "match_paths": list(snapshot.match_paths),
        "endpoints": list(snapshot.endpoints),
        "device_paths": list(snapshot.device_paths),
        "lock_paths": list(snapshot.lock_paths),
        "captured_at": snapshot.captured_at,
    }


def _round_report(result: RoundResult) -> dict[str, Any]:
    return {
        "round": result.round,
        "order": list(result.order),
        "diff": _jsonable(result.diff),
        "verdict": result.verdict,
        "reason_code": result.reason_code,
        "notes": list(result.notes),
    }


def _authorization_report(manifest: AbManifest) -> dict[str, Any] | None:
    authorization = manifest.authorization
    if authorization is None:
        return None
    return {
        "allow_destructive": authorization.allow_destructive,
        "decision_ref": authorization.decision_ref,
        "boards": list(authorization.boards),
    }


def _tool_revision(run: Runner, repo_root: Path) -> str | None:
    try:
        result = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], timeout=15)
    except Exception:
        return None
    value = str(getattr(result, "stdout", "")).strip()
    if (int(getattr(result, "rc", -1)) != 0 or bool(getattr(result, "timed_out", False))
            or re.fullmatch(r"[0-9a-f]{40}", value) is None):
        return None
    return value


def _file_digest(path: Path) -> str | None:
    try:
        return jsonio.sha256_file(path)
    except OSError:
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    """讀取比較器輸入；缺檔、無效 JSON 或頂層非物件均 fail closed。"""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("HARNESS_COMPARATOR_ERROR", f"無法讀取 JSON 物件 {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise HarnessError("HARNESS_COMPARATOR_ERROR", f"JSON 頂層非物件：{path}")
    return document


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


__all__ = [
    "AbRunResult",
    "RoundResult",
    "SideResult",
    "VERDICT_PRECEDENCE",
    "run_ab",
]
