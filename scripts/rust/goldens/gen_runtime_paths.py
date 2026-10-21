#!/usr/bin/env python3
"""產生 ``contracts/golden/runtime_paths.posix.tsv`` 與 ``runtime_paths.windows.tsv``。

以 Python 3.12 執行 ``sw_core.constants``，驗證各種環境變數組合下的執行期路徑解析結果。
至少 40 組 env 矩陣（涵蓋各 SERIALWRAP_*_DIR、XDG_*、SERIALWRAP_TCP_PORT 與 SERIALWRAP_ENDPOINT）。

用法::

    python3 scripts/rust/goldens/gen_runtime_paths.py [--out-dir contracts/golden]
    python3 scripts/rust/goldens/gen_runtime_paths.py --out contracts/golden/runtime_paths.posix.tsv
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from _tsv import Row, write_tsv

ROOT = Path(__file__).resolve().parents[3]

POSIX_PROBE = """
import sys, os, json
sys.path.insert(0, sys.argv[1])
try:
    import sw_core.constants as c
    obj = {
        "config_dir": c.CONFIG_DIR,
        "config_yaml": os.path.join(c.CONFIG_DIR, "config.yaml"),
        "data_dir": c.DATA_DIR,
        "default_endpoint": c.DEFAULT_ENDPOINT,
        "default_tcp_port": c.DEFAULT_TCP_PORT,
        "lock_path": c.LOCK_PATH,
        "log_dir": c.LOG_DIR,
        "profile_dir": c.PROFILE_DIR,
        "run_dir": c.RUN_DIR,
        "socket_path": c.SOCKET_PATH,
        "state_dir": c.STATE_DIR,
        "wal_dir": c.WAL_DIR,
    }
    print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
except ValueError:
    print("ERR:ValueError")
"""

WINDOWS_PROBE = """
import sys, os, ntpath, json
import unittest.mock
sys.path.insert(0, sys.argv[1])
with unittest.mock.patch("os.path", ntpath), \\
     unittest.mock.patch("os.name", "nt"), \\
     unittest.mock.patch("sys.platform", "win32"):
    try:
        import sw_core.constants as c
        obj = {
            "config_dir": c.CONFIG_DIR,
            "config_yaml": ntpath.join(c.CONFIG_DIR, "config.yaml"),
            "data_dir": c.DATA_DIR,
            "default_endpoint": c.DEFAULT_ENDPOINT,
            "default_tcp_port": c.DEFAULT_TCP_PORT,
            "lock_path": c.LOCK_PATH,
            "log_dir": c.LOG_DIR,
            "profile_dir": c.PROFILE_DIR,
            "run_dir": c.RUN_DIR,
            "socket_path": c.SOCKET_PATH,
            "state_dir": c.STATE_DIR,
            "wal_dir": c.WAL_DIR,
        }
        print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    except ValueError:
        print("ERR:ValueError")
"""


def build_env_matrix() -> list[tuple[str, dict[str, str]]]:
    cases: list[tuple[str, dict[str, str]]] = []

    # 1. 基準乾淨環境
    cases.append(("default_clean", {}))

    # 2. SERIALWRAP_CONFIG_DIR 各種組合
    cases.append(("config_dir_empty", {"SERIALWRAP_CONFIG_DIR": ""}))
    cases.append(("config_dir_spaces", {"SERIALWRAP_CONFIG_DIR": "   "}))
    cases.append(("config_dir_home", {"SERIALWRAP_CONFIG_DIR": "~/custom_cfg"}))
    cases.append(("config_dir_abs", {"SERIALWRAP_CONFIG_DIR": "/var/sw/config"}))

    # 3. SERIALWRAP_STATE_DIR 各種組合
    cases.append(("state_dir_empty", {"SERIALWRAP_STATE_DIR": ""}))
    cases.append(("state_dir_spaces", {"SERIALWRAP_STATE_DIR": "   "}))
    cases.append(("state_dir_home", {"SERIALWRAP_STATE_DIR": "~/custom_state"}))
    cases.append(("state_dir_abs", {"SERIALWRAP_STATE_DIR": "/var/sw/state"}))

    # 4. SERIALWRAP_RUN_DIR 各種組合
    cases.append(("run_dir_empty", {"SERIALWRAP_RUN_DIR": ""}))
    cases.append(("run_dir_spaces", {"SERIALWRAP_RUN_DIR": "   "}))
    cases.append(("run_dir_home", {"SERIALWRAP_RUN_DIR": "~/custom_run"}))
    cases.append(("run_dir_abs", {"SERIALWRAP_RUN_DIR": "/var/sw/run"}))

    # 5. SERIALWRAP_DATA_DIR 各種組合
    cases.append(("data_dir_empty", {"SERIALWRAP_DATA_DIR": ""}))
    cases.append(("data_dir_spaces", {"SERIALWRAP_DATA_DIR": "   "}))
    cases.append(("data_dir_home", {"SERIALWRAP_DATA_DIR": "~/custom_data"}))
    cases.append(("data_dir_abs", {"SERIALWRAP_DATA_DIR": "/var/sw/data"}))

    # 6. SERIALWRAP_WAL_DIR 各種組合
    cases.append(("wal_dir_empty", {"SERIALWRAP_WAL_DIR": ""}))
    cases.append(("wal_dir_spaces", {"SERIALWRAP_WAL_DIR": "   "}))
    cases.append(("wal_dir_home", {"SERIALWRAP_WAL_DIR": "~/custom_wal"}))
    cases.append(("wal_dir_abs", {"SERIALWRAP_WAL_DIR": "/var/sw/wal"}))

    # 7. SERIALWRAP_PROFILE_DIR 各種組合
    cases.append(("profile_dir_empty", {"SERIALWRAP_PROFILE_DIR": ""}))
    cases.append(("profile_dir_spaces", {"SERIALWRAP_PROFILE_DIR": "   "}))
    cases.append(("profile_dir_home", {"SERIALWRAP_PROFILE_DIR": "~/custom_profiles"}))
    cases.append(("profile_dir_abs", {"SERIALWRAP_PROFILE_DIR": "/var/sw/profiles"}))

    # 8. SERIALWRAP_LOG_DIR 各種組合
    cases.append(("log_dir_empty", {"SERIALWRAP_LOG_DIR": ""}))
    cases.append(("log_dir_spaces", {"SERIALWRAP_LOG_DIR": "   "}))
    cases.append(("log_dir_home", {"SERIALWRAP_LOG_DIR": "~/custom_log"}))
    cases.append(("log_dir_abs", {"SERIALWRAP_LOG_DIR": "/var/sw/log"}))

    # 9. XDG_* 目錄組合
    cases.append(("xdg_config_empty", {"XDG_CONFIG_HOME": ""}))
    cases.append(("xdg_config_custom", {"XDG_CONFIG_HOME": "/custom/xdg_config"}))
    cases.append(("xdg_state_empty", {"XDG_STATE_HOME": ""}))
    cases.append(("xdg_state_custom", {"XDG_STATE_HOME": "/custom/xdg_state"}))
    cases.append(("xdg_data_empty", {"XDG_DATA_HOME": ""}))
    cases.append(("xdg_data_custom", {"XDG_DATA_HOME": "/custom/xdg_data"}))
    cases.append(("xdg_runtime_empty", {"XDG_RUNTIME_DIR": ""}))
    cases.append(("xdg_runtime_custom", {"XDG_RUNTIME_DIR": "/custom/xdg_run"}))

    # 10. RUN_DIR 三段 fallback 測試
    # (a) 只設 SERIALWRAP_STATE_DIR -> RUN_DIR 預設為 STATE_DIR（即使有 XDG_RUNTIME_DIR）
    cases.append((
        "run_fallback_state_overrides_xdg",
        {"SERIALWRAP_STATE_DIR": "/iso/state", "XDG_RUNTIME_DIR": "/iso/run"},
    ))
    # (b) 未設 SERIALWRAP_STATE_DIR 但有 XDG_RUNTIME_DIR -> os.path.join(XDG_RUNTIME_DIR, "serialwrap")
    cases.append(("run_fallback_xdg_runtime", {"XDG_RUNTIME_DIR": "/iso/run"}))
    # (c) 未設 SERIALWRAP_STATE_DIR 且無 XDG_RUNTIME_DIR -> os.path.join(STATE_DIR, "run")
    cases.append(("run_fallback_state_sub_run", {"XDG_RUNTIME_DIR": ""}))

    # 11. SERIALWRAP_TCP_PORT 組合
    cases.append(("tcp_port_empty", {"SERIALWRAP_TCP_PORT": ""}))
    cases.append(("tcp_port_spaces", {"SERIALWRAP_TCP_PORT": " 49000 "}))
    cases.append(("tcp_port_underscore", {"SERIALWRAP_TCP_PORT": "1_0"}))
    cases.append(("tcp_port_abc", {"SERIALWRAP_TCP_PORT": "abc"}))
    cases.append(("tcp_port_huge", {"SERIALWRAP_TCP_PORT": "99999999"}))
    cases.append(("tcp_port_arabic", {"SERIALWRAP_TCP_PORT": "٤٨٧٠٠"}))

    # 12. SERIALWRAP_ENDPOINT 組合
    cases.append(("endpoint_tcp", {"SERIALWRAP_ENDPOINT": "tcp://10.0.0.1:9000"}))
    cases.append(("endpoint_unix", {"SERIALWRAP_ENDPOINT": "/custom/path.sock"}))
    cases.append(("endpoint_empty", {"SERIALWRAP_ENDPOINT": ""}))

    # 13. 複合隔離環境（常見於 CI / throwaway daemon）
    cases.append((
        "isolated_throwaway",
        {
            "SERIALWRAP_CONFIG_DIR": "/tmp/test/cfg",
            "SERIALWRAP_STATE_DIR": "/tmp/test/state",
            "SERIALWRAP_RUN_DIR": "/tmp/test/run",
            "SERIALWRAP_DATA_DIR": "/tmp/test/data",
            "SERIALWRAP_LOG_DIR": "/tmp/test/log",
            "SERIALWRAP_TCP_PORT": "48799",
            "SERIALWRAP_ENDPOINT": "tcp://127.0.0.1:48799",
        },
    ))
    cases.append((
        "isolated_partial",
        {
            "SERIALWRAP_STATE_DIR": "~/iso_state",
            "SERIALWRAP_TCP_PORT": "50000",
        },
    ))
    cases.append((
        "all_empty_strings",
        {
            "SERIALWRAP_CONFIG_DIR": "",
            "SERIALWRAP_STATE_DIR": "",
            "SERIALWRAP_RUN_DIR": "",
            "SERIALWRAP_DATA_DIR": "",
            "SERIALWRAP_WAL_DIR": "",
            "SERIALWRAP_PROFILE_DIR": "",
            "SERIALWRAP_LOG_DIR": "",
            "XDG_CONFIG_HOME": "",
            "XDG_STATE_HOME": "",
            "XDG_DATA_HOME": "",
            "XDG_RUNTIME_DIR": "",
        },
    ))

    return cases


def run_probe(matrix: list[tuple[str, dict[str, str]]], is_windows: bool) -> list[Row]:
    rows: list[Row] = []
    probe_code = WINDOWS_PROBE if is_windows else POSIX_PROBE

    for case_id, user_env in matrix:
        # 建立執行期環境
        env = {
            "HOME": "/golden/pw-home",
            "USERPROFILE": "/golden/pw-home",
            "PATH": os.environ.get("PATH", ""),
        }
        env.update(user_env)

        proc = subprocess.run(
            [sys.executable, "-I", "-c", probe_code, str(ROOT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Probe failed for {case_id} (exit {proc.returncode}):\n{proc.stderr}"
            )

        expected = proc.stdout.strip()
        # 輸入格式為 KEY=VALUE 以 \x1f 串接
        input_str = "\x1f".join(f"{k}={v}" for k, v in user_env.items())
        rows.append((case_id, "resolve", input_str, expected))

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="輸出目錄")
    parser.add_argument("--out", type=Path, help="單一輸出檔名")
    args = parser.parse_args()

    if not args.out_dir and not args.out:
        args.out_dir = ROOT / "contracts/golden"

    matrix = build_env_matrix()

    if args.out:
        out_path = args.out
        is_win = "windows" in out_path.name
        rows = run_probe(matrix, is_windows=is_win)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_tsv(out_path, rows)
        print(f"Wrote {len(rows)} cases to {out_path}")
    else:
        posix_path = args.out_dir / "runtime_paths.posix.tsv"
        win_path = args.out_dir / "runtime_paths.windows.tsv"
        args.out_dir.mkdir(parents=True, exist_ok=True)

        posix_rows = run_probe(matrix, is_windows=False)
        write_tsv(posix_path, posix_rows)
        print(f"Wrote {len(posix_rows)} cases to {posix_path}")

        win_rows = run_probe(matrix, is_windows=True)
        write_tsv(win_path, win_rows)
        print(f"Wrote {len(win_rows)} cases to {win_path}")


if __name__ == "__main__":
    main()
