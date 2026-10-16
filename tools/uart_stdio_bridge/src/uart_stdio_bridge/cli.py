"""uart-stdio-bridge 命令列介面入口。"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys

from uart_stdio_bridge import TOOL_NAME, WIRE_SCHEMA, __version__
from uart_stdio_bridge.identity import tool_tree_sha256

_SUBCOMMAND_TARGETS = {
    "dut": "uart_stdio_bridge.dut.__main__",
    "run": "uart_stdio_bridge.bridge.core",
    "compare": "uart_stdio_bridge.compare.comparator",
    "redact": "uart_stdio_bridge.compare.redact",
    "selftest": "uart_stdio_bridge.selftest.runner",
}


def _run_stub(subcommand: str) -> int:
    err_obj = {"error": "not_implemented", "subcommand": subcommand}
    sys.stderr.write(json.dumps(err_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 2


def _run_command(argv: list[str]) -> int:
    """建立 bridge 元件並執行 run 子命令。"""
    import os
    import pathlib
    import re
    import secrets

    from uart_stdio_bridge.bridge.control import ControlServer, write_control_info
    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.bridge.dut_process import DutProcess
    from uart_stdio_bridge.dut.control_file import DutControlClient
    from uart_stdio_bridge.endpoints import open_endpoint
    from uart_stdio_bridge.evidence.clock import clock_id
    from uart_stdio_bridge.evidence.manifest import ensure_private_dir
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    parser = argparse.ArgumentParser(prog="uart-stdio-bridge run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--port-id", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--endpoint", choices=("posix-pty", "win-serial"), required=True)
    parser.add_argument("--by-id-link")
    parser.add_argument("--com")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--transport-mode",
        choices=("virtual_pty", "virtual_com0com", "physical_usb_uart"),
        default=None,
    )
    parser.add_argument("--allow-peer-close", action="store_true")
    parser.add_argument("--no-peer-gate", action="store_true")
    parser.add_argument("--dut-mode", choices=("local-process", "docker"), required=True)
    parser.add_argument("--dut-argv-json")
    parser.add_argument("--dut-image")
    parser.add_argument("--dut-target-json")
    parser.add_argument("--dut-plan-json")
    parser.add_argument("--dut-seed", type=int)
    parser.add_argument("--dut-cwd")
    parser.add_argument("--max-inflight-bytes", type=int, default=1_048_576)
    parser.add_argument("--read-chunk", type=int, default=4096)
    parser.add_argument("--write-timeout-s", type=float, default=5.0)
    parser.add_argument("--allow-dut-exit", action="store_true")
    args = parser.parse_args(argv)

    evidence_dir = pathlib.Path(args.evidence_dir).resolve()
    if args.endpoint == "posix-pty" and not args.by_id_link:
        parser.error("posix-pty 必須提供 --by-id-link")
    if args.endpoint == "win-serial" and not args.com:
        parser.error("win-serial 必須提供 --com")
    if args.dut_mode == "local-process" and not args.dut_argv_json and not args.dut_target_json:
        parser.error("local-process 必須提供 --dut-argv-json 或 --dut-target-json")
    if args.dut_mode == "docker" and not args.dut_image:
        parser.error("docker 必須提供 --dut-image")
    if args.dut_mode == "local-process" and args.dut_image:
        parser.error("local-process 不接受 --dut-image")
    if args.dut_mode == "docker" and args.dut_argv_json:
        parser.error("docker 不接受 --dut-argv-json")

    ensure_private_dir(evidence_dir)
    if args.endpoint == "posix-pty":
        endpoint_spec = {
            "kind": "posix-pty",
            "by_id_link": args.by_id_link,
            "peer_gate": not args.no_peer_gate,
        }
    else:
        transport_mode = args.transport_mode or "virtual_com0com"
        if transport_mode == "virtual_pty":
            parser.error("win-serial 的 transport-mode 不得是 virtual_pty")
        endpoint_spec = {
            "kind": "win-serial",
            "com": args.com,
            "baud": args.baud,
            "transport_mode": transport_mode,
            "allow_peer_close": args.allow_peer_close,
        }

    dut_control_dir = evidence_dir / "dut-control"
    dut_input_dir = evidence_dir / "dut-input"
    ensure_private_dir(dut_control_dir)

    def preserve_input(source: str | bytes, name: str) -> pathlib.Path:
        destination = dut_input_dir / name
        if isinstance(source, (bytes, bytearray)):
            payload = bytes(source)
        else:
            payload = pathlib.Path(source).resolve().read_bytes()
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        if os.name != "nt":
            os.chmod(destination, 0o600)
        return destination

    if args.dut_mode == "local-process":
        from uart_stdio_bridge.bridge.dut_modes import local_process_argv

        copied_target: pathlib.Path | None = None
        copied_plan: pathlib.Path | None = None
        if args.dut_target_json or args.dut_plan_json:
            ensure_private_dir(dut_input_dir)
        if args.dut_target_json:
            copied_target = preserve_input(args.dut_target_json, "target.json")
        if args.dut_plan_json:
            copied_plan = preserve_input(args.dut_plan_json, "plan.json")

        if args.dut_argv_json:
            try:
                dut_argv = json.loads(args.dut_argv_json)
            except json.JSONDecodeError as exc:
                parser.error(f"--dut-argv-json 不是有效 JSON：{exc}")
            if not isinstance(dut_argv, list) or not dut_argv or not all(
                isinstance(item, str) for item in dut_argv
            ):
                parser.error("--dut-argv-json 必須是非空字串陣列")

            def replace_input_arg(name: str, value: pathlib.Path) -> None:
                for index, item in enumerate(dut_argv):
                    if item == name:
                        if index + 1 >= len(dut_argv):
                            dut_argv.append(str(value))
                        else:
                            dut_argv[index + 1] = str(value)
                        return
                    if item.startswith(name + "="):
                        dut_argv[index] = f"{name}={value}"
                        return
                dut_argv.extend([name, str(value)])

            if copied_target:
                replace_input_arg("--target-json", copied_target)
            if copied_plan:
                replace_input_arg("--plan-json", copied_plan)
            if args.dut_seed is not None and "--seed" not in dut_argv:
                dut_argv.extend(["--seed", str(args.dut_seed)])
            uses_dut_control = bool(
                copied_target
                or copied_plan
                or ("--mode" in dut_argv and "model" in dut_argv)
            )
            if uses_dut_control and "--control-dir" not in dut_argv:
                dut_argv.extend(["--control-dir", str(dut_control_dir)])
        else:
            assert copied_target is not None, "local-process 必須提供 --dut-target-json 或 --dut-argv-json"
            src_root = pathlib.Path(__file__).resolve().parents[1]
            seed = args.dut_seed if args.dut_seed is not None else 0
            dut_argv = local_process_argv(
                src_root,
                target_json=copied_target,
                plan_json=copied_plan,
                control_dir=dut_control_dir,
                seed=seed,
            )
            uses_dut_control = True
    else:
        from uart_stdio_bridge.bridge.dut_modes import docker_argv, docker_image_id
        from uart_stdio_bridge.dut.model import TargetConfig

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id):
            parser.error(f"--run-id 含有無效字元（Docker 容器名稱限制）：{args.run_id}")

        ensure_private_dir(dut_input_dir)
        scenario_dir = dut_input_dir

        if args.dut_target_json:
            preserve_input(args.dut_target_json, "target.json")
        else:
            target_data = json.dumps(TargetConfig().to_json(), ensure_ascii=False).encode("utf-8")
            preserve_input(target_data, "target.json")

        has_plan = False
        if args.dut_plan_json:
            preserve_input(args.dut_plan_json, "plan.json")
            has_plan = True

        seed = args.dut_seed if args.dut_seed is not None else 0
        img_id = docker_image_id(args.dut_image)
        if not img_id:
            raise OSError(f"HARNESS_DUT_SPAWN_FAILED: docker image not found: {args.dut_image}")
        uses_dut_control = True

    endpoint = open_endpoint(endpoint_spec)
    server: ControlServer | None = None
    control: DutControlClient | None = None
    bridge: Bridge | None = None
    try:
        if uses_dut_control:
            control = DutControlClient(dut_control_dir)
        if args.dut_mode == "docker":
            dut = DutProcess(
                lambda g: docker_argv(
                    img_id,
                    run_id=args.run_id,
                    generation=g,
                    scenario_dir=scenario_dir,
                    control_dir=dut_control_dir,
                    seed=seed,
                    has_plan=has_plan,
                ),
                mode="docker",
                cwd=args.dut_cwd,
                image=args.dut_image,
                image_id=img_id,
            )
        else:
            dut = DutProcess(
                lambda _generation: list(dut_argv),
                mode=args.dut_mode,
                cwd=args.dut_cwd,
            )
        stderr_cap = 268_435_456
        test_cap = os.environ.get("UART_STDIO_BRIDGE_TEST_STDERR_CAP")
        if test_cap is not None:
            stderr_cap = int(test_cap)
        os_write_fn = os.write
        if os.environ.get("UART_STDIO_BRIDGE_TEST_FAULT") == "enospc_after_1mib":
            import errno
            fault_bytes_written = [0]
            def _fault_os_write(fd: int, data: bytes) -> int:
                if fault_bytes_written[0] + len(data) > 1024 * 1024:
                    raise OSError(errno.ENOSPC, "No space left on device")
                n = os.write(fd, data)
                fault_bytes_written[0] += n
                return n
            os_write_fn = _fault_os_write
        writer = EvidenceWriter(
            evidence_dir / "wire.ndjson",
            run_id=args.run_id,
            case_id=args.case_id,
            clock_id=clock_id(),
            os_write=os_write_fn,
        )
        config = BridgeConfig(
            run_id=args.run_id,
            case_id=args.case_id,
            port_id=args.port_id,
            evidence_dir=evidence_dir,
            read_chunk=args.read_chunk,
            max_inflight_bytes=args.max_inflight_bytes,
            write_timeout_s=args.write_timeout_s,
            stderr_cap_bytes=stderr_cap,
            allow_dut_exit=args.allow_dut_exit,
            test_overrides={
                name: value
                for name, value in os.environ.items()
                if name.startswith("UART_STDIO_BRIDGE_TEST_")
            },
        )
        bridge = Bridge(config, endpoint, dut, writer, control)
        bridge.start()
        token = secrets.token_urlsafe(32)
        server = ControlServer(bridge.handle_action, token=token)
        host, port = server.start()
        control_info = evidence_dir / "control.json"
        write_control_info(control_info, host=host, port=port, token=token)
        ready = {
            "control_info": str(control_info.resolve()),
            "event": "BRIDGE_READY",
            "port_id": args.port_id,
        }
        if args.endpoint == "posix-pty":
            ready["slave_path"] = getattr(endpoint, "slave_path", None)
        else:
            ready["com"] = args.com
        sys.stdout.write(
            json.dumps(ready, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        sys.stdout.flush()
        try:
            return bridge.wait()
        finally:
            server.stop()
    except Exception:
        if bridge is None:
            try:
                endpoint.close()
            except Exception:
                pass
        if server is not None:
            server.stop()
        raise


def _run_command_safe(argv: list[str]) -> int:
    try:
        return _run_command(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code.startswith("HARNESS_"):
            if isinstance(exc, ModuleNotFoundError) and str(getattr(exc, "name", "")).startswith(
                "uart_stdio_bridge.evidence"
            ):
                code = "HARNESS_EVIDENCE_WRITE_FAILED"
            elif isinstance(exc, (OSError, subprocess.SubprocessError)):
                code = "HARNESS_DUT_SPAWN_FAILED"
            else:
                code = "HARNESS_CONTROL_PROTOCOL_ERROR"
        err_obj = {"code": code, "error": str(exc)}
        sys.stderr.write(
            json.dumps(err_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        sys.stderr.flush()
        return 3


def _version_cmd() -> int:
    try:
        sha = tool_tree_sha256()
    except Exception as e:
        err_obj = {"error": "insufficient_evidence", "detail": str(e)}
        sys.stderr.write(json.dumps(err_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 2
    obj = {
        "name": TOOL_NAME,
        "tree_sha256": sha,
        "version": __version__,
        "wire_schema": WIRE_SCHEMA,
    }
    sys.stdout.write(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def _dispatch(subcommand: str, rest: list[str]) -> int:
    if subcommand == "run":
        return _run_command_safe(rest)
    target_mod = _SUBCOMMAND_TARGETS.get(subcommand)
    if target_mod:
        try:
            mod = importlib.import_module(target_mod)
        except ModuleNotFoundError as e:
            # 僅當缺失的模組為目標模組自身或其套件前綴時，才判定為尚未實作
            if e.name == target_mod or target_mod.startswith(f"{e.name}."):
                return _run_stub(subcommand)
            raise
        sub_main = getattr(mod, "main", None)
        if callable(sub_main):
            return int(sub_main(rest))
    return _run_stub(subcommand)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uart-stdio-bridge",
        description="serialwrap 外部 UART 轉接 bridge 與測試工具",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # version 子命令
    subparsers.add_parser("version", help="顯示工具身分與 schema 版本（JSON 格式）")

    subparsers.add_parser("run", add_help=False, help="執行 bridge 轉接與錄證")
    subparsers.add_parser("compare", add_help=False, help="WAL 與 wire byte stream 比對、窗口判定與竄改拒絕")
    subparsers.add_parser("redact", add_help=False, help="產生遮罩衍生證據目錄與 redaction-manifest.json")
    subparsers.add_parser("dut", add_help=False, help="執行 DUT 模擬器")
    subparsers.add_parser("selftest", add_help=False, help="執行 bridge 自測與資格報告")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)

    if args.subcommand == "version":
        if rest:
            parser.error(f"unrecognized arguments: {' '.join(rest)}")
        return _version_cmd()

    return _dispatch(args.subcommand, rest)
