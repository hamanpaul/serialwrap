"""bridge 自測執行器與資格判定（§15.4.3／Spike 4）。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import platform
from typing import Any, Sequence

from uart_stdio_bridge import TOOL_NAME, __version__
from uart_stdio_bridge.identity import tool_tree_sha256
from uart_stdio_bridge.selftest.checks import (
    CHECK_IDS,
    CheckResult,
    SelftestContext,
    run_check,
)


def run_selftest(ctx: SelftestContext, *, only: Sequence[str] | None = None) -> dict[str, Any]:
    """執行 bridge 自測項目並產生 bridge-selftest.v1 格式報告字典。"""
    if only is not None:
        target_ids = [x.strip() for x in only if x.strip()]
        if not target_ids:
            raise ValueError("only 清單不得為空")
        for cid in target_ids:
            if cid not in CHECK_IDS:
                raise ValueError(f"未知之檢查 ID: {cid}")
    else:
        target_ids = list(CHECK_IDS)

    results: list[CheckResult] = []
    for check_id in target_ids:
        res = run_check(check_id, ctx)
        results.append(res)

    cap_transport = bool(ctx.endpoint == "posix-pty")

    # 判定總體結果
    all_pass = True
    for r in results:
        if r.id == "ST-12" and ctx.endpoint == "win-serial" and not cap_transport:
            if r.verdict not in ("PASS", "NOT_RUN"):
                all_pass = False
                break
        else:
            if r.verdict != "PASS":
                all_pass = False
                break

    from uart_stdio_bridge.evidence.manifest import platform_info
    p_info = platform_info(
        endpoint=ctx.endpoint,
        com_driver=ctx.com_driver,
        com_driver_install_method=ctx.com_driver_install_method,
    )
    p_info["evidence_layer"] = "native-windows" if ctx.endpoint == "win-serial" else "posix-pty"
    p_info["com_pair"] = list(ctx.com_pair) if ctx.com_pair else None

    complete = len(results) == len(CHECK_IDS)
    not_selected = [cid for cid in CHECK_IDS if cid not in target_ids]

    report: dict[str, Any] = {
        "schema_version": "bridge-selftest.v1",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": complete,
        "not_selected": not_selected,
        "tool": {
            "name": TOOL_NAME,
            "version": __version__,
            "tree_sha256": tool_tree_sha256(),
        },
        "platform": p_info,
        "endpoint": ctx.endpoint,
        "transport_mode": ctx.transport_mode,
        "capabilities": {
            "transport_disconnect": cap_transport,
        },
        "checks": [
            {
                "id": r.id,
                "verdict": r.verdict,
                "reason": r.reason,
                "evidence_dir": r.evidence_dir,
            }
            for r in results
        ],
        "verdict": "PASS" if all_pass else "FAIL",
        "test_overrides": {
            k: v for k, v in os.environ.items() if k.startswith("UART_STDIO_BRIDGE_TEST_")
        },
    }

    return report


def load_approved_revision(path: pathlib.Path | None = None) -> dict[str, Any]:
    """載入 APPROVED_REVISION.json 並檢驗必備欄位。"""
    if path is None:
        path = pathlib.Path(__file__).resolve().parents[3] / "APPROVED_REVISION.json"
    else:
        path = pathlib.Path(path)

    if not path.is_file():
        raise ValueError(f"APPROVED_REVISION.json 不存在: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"解析 APPROVED_REVISION.json 失敗: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("APPROVED_REVISION.json 必須是 JSON 字典")

    for field in ("schema_version", "tree_sha256", "reviewer", "review_ref", "date"):
        val = data.get(field)
        if not val or not isinstance(val, str) or not val.strip():
            raise ValueError(f"APPROVED_REVISION.json 欄位 {field} 缺失或為空")

    if data.get("schema_version") != "bridge-approved.v1":
        raise ValueError("schema_version 必須是 bridge-approved.v1")

    from datetime import date
    try:
        date.fromisoformat(data["date"])
    except Exception as exc:
        raise ValueError(f"date 欄位非有效 ISO 日期: {data['date']}") from exc

    return data


def is_qualified(
    report: dict[str, Any],
    *,
    endpoint: str,
    tool_tree_sha256: str,
    platform_os: str,
    approved: dict[str, Any],
) -> bool:
    """依 §15.4.3／ADR-08 判定自測報告是否具備合格資格。"""
    try:
        if not isinstance(report, dict):
            return False
        if report.get("schema_version") != "bridge-selftest.v1":
            return False
        if report.get("complete") is False:
            return False
        if report.get("endpoint") != endpoint:
            return False
        platform_dict = report.get("platform")
        if not isinstance(platform_dict, dict) or platform_dict.get("os") != platform_os:
            return False

        report_tool = report.get("tool")
        if not isinstance(report_tool, dict):
            return False
        report_tool_sha = report_tool.get("tree_sha256")
        approved_sha = approved.get("tree_sha256") if isinstance(approved, dict) else None

        import re
        hex64 = re.compile(r"^[0-9a-f]{64}$")
        if not (
            report_tool_sha and hex64.match(report_tool_sha) and
            approved_sha and hex64.match(approved_sha) and
            tool_tree_sha256 and hex64.match(tool_tree_sha256) and
            report_tool_sha == approved_sha == tool_tree_sha256
        ):
            return False

        # test_overrides 必須為空
        test_overrides = report.get("test_overrides")
        if test_overrides:
            return False

        if report.get("verdict") != "PASS":
            return False

        checks = report.get("checks")
        if not isinstance(checks, list) or len(checks) != len(CHECK_IDS):
            return False

        if [c.get("id") for c in checks if isinstance(c, dict)] != list(CHECK_IDS):
            return False

        capabilities = report.get("capabilities")
        if not isinstance(capabilities, dict):
            return False
        cap_transport = capabilities.get("transport_disconnect")

        check_map = {c.get("id"): c for c in checks if isinstance(c, dict)}
        for check_id in CHECK_IDS:
            c = check_map.get(check_id)
            if not c:
                return False
            verdict = c.get("verdict")
            if check_id == "ST-12" and endpoint == "win-serial" and cap_transport is False:
                if verdict not in ("PASS", "NOT_RUN"):
                    return False
            else:
                if verdict != "PASS":
                    return False

        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    """selftest 子命令入口。"""
    parser = argparse.ArgumentParser(
        prog="uart-stdio-bridge selftest",
        description="執行 bridge 自測與資格報告",
    )
    parser.add_argument("--endpoint", choices=("posix-pty", "win-serial"), required=True)
    parser.add_argument("--com-pair", help="win-serial 專用：COMa:COMb")
    parser.add_argument(
        "--transport-mode",
        choices=("virtual_pty", "virtual_com0com", "physical_usb_uart"),
        default=None,
    )
    parser.add_argument("--out", required=True, help="報告輸出目錄")
    parser.add_argument("--only", help="只執行指定檢查項目，逗號分隔，例如 ST-01,ST-02")
    parser.add_argument("--seed", type=int, default=20261015, help="隨機種子")
    parser.add_argument("--com-driver-name")
    parser.add_argument("--com-driver-version")
    parser.add_argument("--com-driver-sha256")
    parser.add_argument("--com-driver-install-method")

    args = parser.parse_args(argv)

    if args.endpoint == "win-serial" and not args.com_pair:
        parser.error("win-serial 必須提供 --com-pair")

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    com_pair: tuple[str, str] | None = None
    if args.com_pair:
        parts = args.com_pair.split(":")
        if len(parts) != 2:
            parser.error("--com-pair 格式必須為 COMa:COMb")
        com_pair = (parts[0], parts[1])

    transport_mode = args.transport_mode
    if transport_mode is None:
        transport_mode = "virtual_pty" if args.endpoint == "posix-pty" else "virtual_com0com"

    only_list: list[str] | None = None
    if args.only is not None:
        only_list = [x.strip() for x in args.only.split(",") if x.strip()]
        if not only_list:
            parser.error("--only 指定之清單不得為空")
        for cid in only_list:
            if cid not in CHECK_IDS:
                parser.error(f"未知之檢查 ID: {cid}")

    work_dir = out_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    ctx = SelftestContext(
        endpoint=args.endpoint,
        com_pair=com_pair,
        transport_mode=transport_mode,
        work_dir=work_dir,
        seed=args.seed,
        com_driver={
            "name": args.com_driver_name,
            "version": args.com_driver_version,
            "sha256": args.com_driver_sha256,
        },
        com_driver_install_method=args.com_driver_install_method,
    )

    report = run_selftest(ctx, only=only_list)

    report_path = out_dir / "selftest-report.json"
    report_json = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    report_path.write_text(report_json, encoding="utf-8")

    return 0 if report.get("verdict") == "PASS" else 1
