"""DUT 模擬器 CLI 入口點（python -m uart_stdio_bridge.dut）。

提供標準輸入輸出 DUT 模擬與純二進位回顯模式，
stdout 只輸出 DUT bytes，診斷日誌一律以 JSON lines 輸出至 stderr。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys

from uart_stdio_bridge.dut.control_file import ControlFileChannel
from uart_stdio_bridge.dut.model import DutModel, TargetConfig
from uart_stdio_bridge.dut.plan import ScenarioPlan
from uart_stdio_bridge.dut.stdio_adapter import run_echo, run_stdio


class _DutArgumentParser(argparse.ArgumentParser):
    """自訂 ArgumentParser，確保命令列參數錯誤時一律輸出 JSON 至 stderr 並以 code 2 退出。"""

    def error(self, message: str) -> None:
        line = json.dumps({"error": message}, ensure_ascii=False) + "\n"
        sys.stderr.write(line)
        sys.stderr.flush()
        sys.exit(2)


def _error_exit(msg: str) -> int:
    line = json.dumps({"error": msg}, ensure_ascii=False) + "\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI 主流程。接受選擇性 argv 參數，回傳行程結束代碼。"""
    if sys.platform == "win32":
        try:
            import msvcrt

            for fd in (0, 1, 2):
                msvcrt.setmode(fd, os.O_BINARY)  # type: ignore[attr-defined]
        except Exception as exc:
            return _error_exit(f"msvcrt.setmode 失敗: {exc}")

    parser = _DutArgumentParser(description="DUT 模擬器")
    parser.add_argument(
        "--mode",
        choices=["dut", "model", "echo"],
        default="dut",
        help="執行模式（dut: DUT 模擬，需 --target-json；model: DUT 模擬，省略 --target-json 時用預設 TargetConfig；echo: 二進位回顯）",
    )
    parser.add_argument("--target-json", help="TargetConfig JSON 檔案路徑")
    parser.add_argument("--plan-json", help="ScenarioPlan JSON 檔案路徑")
    parser.add_argument("--control-dir", help="控制通道目錄路徑")
    parser.add_argument("--seed", type=int, help="隨機種子（預留供 §15.7.1 隨機故障注入使用）")
    parser.add_argument(
        "--noise-interval-ms",
        type=int,
        default=None,
        help="覆寫 TargetConfig 雜訊間隔 (ms) 並啟用雜訊（dut／model 模式）",
    )

    # echo 模式參數
    parser.add_argument("--stderr-flood-bytes", type=int, default=0, help="stderr 注入 byte 數")
    parser.add_argument("--exit-after-bytes", type=int, default=None, help="輸出指定 byte 數後退出")
    parser.add_argument("--exit-code", type=int, default=0, help="exit-after-bytes 觸發時的 exit code")
    parser.add_argument(
        "--close-stdout-after-bytes",
        type=int,
        default=None,
        help="輸出指定 byte 數後關閉 stdout",
    )
    parser.add_argument("--read-delay-ms", type=int, default=0, help="每次讀取延遲 (ms)")
    parser.add_argument("--banner", default=None, help="啟動即寫到 stdout 的 banner 文字（UTF-8；echo 模式）")

    try:
        args = parser.parse_args(argv)
    except SystemExit as se:
        return int(se.code) if se.code is not None else 2
    except Exception as exc:
        return _error_exit(str(exc))

    try:
        if args.mode in ("dut", "model"):
            if (
                args.stderr_flood_bytes != 0
                or args.exit_after_bytes is not None
                or args.exit_code != 0
                or args.close_stdout_after_bytes is not None
                or args.read_delay_ms != 0
                or args.banner is not None
            ):
                return _error_exit("echo 模式專屬參數不得於 dut 模式使用")

            if args.target_json:
                target_path = pathlib.Path(args.target_json)
                if not target_path.exists():
                    return _error_exit(f"檔案不存在: {args.target_json}")

                try:
                    raw_target = json.loads(target_path.read_text(encoding="utf-8"))
                    target_cfg = TargetConfig.from_json(raw_target)
                except Exception as exc:
                    return _error_exit(f"TargetConfig JSON 解析失敗: {exc}")
            elif args.mode == "model":
                target_cfg = TargetConfig()
            else:
                return _error_exit("缺少 --target-json 參數")

            if args.noise_interval_ms is not None:
                if args.noise_interval_ms < 1:
                    return _error_exit("--noise-interval-ms 必須 >= 1")
                target_cfg = dataclasses.replace(
                    target_cfg, noise_enabled=True, noise_interval_ms=args.noise_interval_ms
                )

            plan: ScenarioPlan | None = None
            if args.plan_json:
                plan_path = pathlib.Path(args.plan_json)
                if not plan_path.exists():
                    return _error_exit(f"計畫檔案不存在: {args.plan_json}")
                try:
                    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    plan = ScenarioPlan.from_json(raw_plan)
                except Exception as exc:
                    return _error_exit(f"ScenarioPlan JSON 解析失敗: {exc}")

            control: ControlFileChannel | None = None
            if args.control_dir:
                control = ControlFileChannel(pathlib.Path(args.control_dir))

            model = DutModel(target_cfg, plan=plan)
            return run_stdio(model, control=control)

        # echo 模式
        if (
            args.target_json is not None
            or args.plan_json is not None
            or args.control_dir is not None
            or args.seed is not None
            or args.noise_interval_ms is not None
        ):
            return _error_exit("dut 模式專屬參數不得於 echo 模式使用")

        return run_echo(
            stderr_flood_bytes=args.stderr_flood_bytes,
            exit_after_bytes=args.exit_after_bytes,
            exit_code=args.exit_code,
            close_stdout_after_bytes=args.close_stdout_after_bytes,
            read_delay_ms=args.read_delay_ms,
            banner=args.banner.encode("utf-8") if args.banner is not None else b"",
        )
    except Exception as exc:
        return _error_exit(f"執行期錯誤: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
