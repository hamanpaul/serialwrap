"""P7 Task 18（08-slice23-login-events.md）：延遲與背景雜訊黑箱。

`test_login_with_delay_and_noise`：每個回應延遲 800 ms＋每 50 ms 一行 kernel noise 下仍能登入到 READY。
雜訊行以 `\r\n` 起頭（printk 獨立成行）：Python 基線實測（2026-10-29）中，不換行直接接在 `login: ` 之後的雜訊
會使 prompt 行永遠比對不到、無法登入（500 ms 間隔、無延遲亦然），故該寫法不是 Python 的可通過情境（案例設計
修正、不改契約語意，記於 p7-fix-responses 第 11 輪）。
`test_control_latency_under_flood_with_events`（計畫同列）：OD-59（擁有者裁決 (a)，rulings/p5.md、p7.md）不新增
`control_latency_flood_p99_ms`，改以 `contracts/resource_limits.yaml` `event_latency` 的 flood 規則判定——
stop_request／command_cancel 取 `max_ms_flood`（100 ms）、`gate_rule: every_run_max_le_max_ms`，量測工具與口徑沿用
`scripts/rust/event_latency_measure.py`（`_Rpc.timed` 的「RPC 送出 → 回應」、`_Flood` 的 20000 B/s 負載、
`flood_problems` 的負載驗證、`judge_run` 的單輪判定與 loadavg 作廢規則）。差別只在負載期間另載一條比對每一行洪水
的規則（scope any、handler 執行中，後續比對被 `per_rule_busy` 丟棄），並斷言 `event_dropped` 有出現且筆數不超過送出
的洪水行數（有界）。daemon RSS 依 OD-09 不作 gate，本案不量。案例設計修正記於 `slice23-chg-register.md`
CHG-S23-CTRL-LATENCY-OD59-01。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import P7Rig, _session_row, wait_state

try:
    import ab_env
    from ab_env import assert_daemon_artifact
except ImportError:
    tests_dir = str(Path(__file__).resolve().parents[2] / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import ab_env
    from ab_env import assert_daemon_artifact

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[2]
CANCEL_SAMPLES = int(os.environ.get("P7_OD59_CANCEL_SAMPLES", "200"))
STOP_SAMPLES = int(os.environ.get("P7_OD59_STOP_SAMPLES", "20"))
OD59_REPEATS = int(os.environ.get("P7_OD59_REPEATS", "1"))
REQUIRE_OD59_CONTRACT = os.environ.get("P7_OD59_REQUIRE_CONTRACT") == "1"
VOID_RETRIES = 2            # 同 event_latency_measure.py 預設：loadavg 峰值 > CPU 數的輪次作廢重跑
FLOOD_MARK = "LATFLOOD"     # `_Flood` 每行開頭


def _measure_tool():
    spec = importlib.util.spec_from_file_location("event_latency_measure",
                                                  ROOT / "scripts" / "rust" / "event_latency_measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("event_latency_measure", mod)
    spec.loader.exec_module(mod)
    return mod


def _add_all_rule(d, tmp: Path) -> None:
    """比對每一行洪水的規則；handler 執行 0.2 s，期間的後續比對由 dispatcher 以 per_rule_busy 丟棄。"""
    rule = {"schema_version": 1, "owner": "p7", "name": "flood-all", "kind": "tool", "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": FLOOD_MARK}, "scope": "any",
            "handler": {"exec": [sys.executable, "-c", "import time; time.sleep(0.2)"]},
            "auto_enable_com_on_load": True, "timeout_ms": 3000}
    path = tmp / "flood-all.json"
    path.write_text(json.dumps(rule), encoding="utf-8")
    r = d.sw("event", "add", "--file", str(path))
    assert r["_rc"] == 0 and r.get("ok") is True, r


def _judged(tool, values, errors, load_problems, *, want, limit, before, monitor):
    return tool.judge_run(values, want=want, limit=limit, load_before=before, load_after=tool.host_load(),
                          errors=errors, load_problems=load_problems, host_samples=monitor.samples)


def _cancel_run(tool, harness, root: Path, limit: float, flood_cfg: dict) -> tuple[dict, dict]:
    """在途命令的 command.cancel（每輪 CANCEL_SAMPLES 筆）；整輪 DUT 持續洪水且 ALL 規則載入中。"""
    rate, chunk, seconds = int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"]), float(flood_cfg["seconds"])
    values: list[float] = []
    errors: list[str] = []
    before = tool.host_load()
    monitor = tool._HostMonitor()
    with monitor:
        d = harness.start_daemon(root, login=False)
        rpc = tool._Rpc(d.endpoint)
        try:
            tool._wait_state(rpc, "READY")
            _add_all_rule(d, root)
            fl = tool._Flood(d.targets["COM0"], rate, chunk)
            with fl:
                for n in range(CANCEL_SAMPLES):
                    try:
                        values.append(tool._sample_cancel(rpc))
                    except Exception as exc:  # noqa: BLE001 — 逐筆記錄（judge_run 判證據不足），不中斷整輪
                        errors.append(f"command_cancel #{n}: {type(exc).__name__}: {exc}")
                        if len(errors) > 5:
                            break
                if fl.elapsed() < seconds:
                    fl.stop_evt.wait(seconds - fl.elapsed())       # 每輪 flood 至少持續 seconds
            stats = fl.stats()
            lines_sent = stats["sent"] // chunk
            entries = d.rpc("event.tail", {"n": 1000000}, timeout=30.0).get("entries") or []
        finally:
            rpc.close()
            d.stop()
    problems = tool.flood_problems(stats, bytes_per_s=rate, min_seconds=seconds)
    run = _judged(tool, values, errors, problems, want=CANCEL_SAMPLES, limit=limit, before=before, monitor=monitor)
    dropped = [e for e in entries if e.get("type") == "event_dropped"]
    events = {"dropped": len(dropped), "lines_sent": lines_sent,
              "dropped_reasons": sorted({str(e.get("reason")) for e in dropped}),
              "dropped_selectors": sorted({str(e.get("selector")) for e in dropped}),
              "matched": sum(e.get("type") == "match_recorded" for e in entries)}
    return run, events


def _stop_run(tool, harness, root: Path, limit: float, flood_cfg: dict) -> dict:
    """daemon.stop（每樣本一個新 daemon，READY＋ALL 規則載入後以 flood 暖機 stop_warmup_s 再送出）。"""
    rate, chunk, warmup = int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"]), float(flood_cfg["stop_warmup_s"])
    values: list[float] = []
    errors: list[str] = []
    problems: list[str] = []
    before = tool.host_load()
    monitor = tool._HostMonitor()
    with monitor:
        for n in range(STOP_SAMPLES):
            sub = root / f"s{n % 2}"
            d = harness.start_daemon(sub, login=False)
            rpc = tool._Rpc(d.endpoint)
            try:
                tool._wait_state(rpc, "READY")
                _add_all_rule(d, sub)
                fl = tool._Flood(d.targets["COM0"], rate, chunk)
                with fl:
                    fl.stop_evt.wait(warmup)                # 暖機：daemon 已在持續 RX＋規則比對下才送 stop
                    ms, r = rpc.timed("daemon.stop", {})
                bad = tool.flood_problems(fl.stats(), bytes_per_s=rate, min_seconds=warmup)
                if bad:
                    problems.append(f"stop #{n}：{'；'.join(bad)}")
                if r.get("ok") is not True:
                    raise RuntimeError(f"daemon.stop 非 ok：{r}")
                values.append(ms)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"stop_request #{n}: {type(exc).__name__}: {exc}")
                if len(errors) > 5:
                    break
            finally:
                rpc.close()
                d.stop()
    return _judged(tool, values, errors, problems, want=STOP_SAMPLES, limit=limit, before=before, monitor=monitor)


def resolve_rust_artifacts(
    *,
    caller_daemon: str | None = None,
    caller_cli: str | None = None,
    cargo_target_dir: str | Path | None = None,
    repo_root: Path = ROOT,
) -> tuple[Path, Path]:
    """解析受測 Rust CLI 與 daemon 執行檔路徑（OD-81／DF-27／P8-a190a57-1）。

    優先順序：
    1. 呼叫端明確指定且非 .py 的 caller_daemon 與 caller_cli。
    2. 若未指定，依 cargo_target_dir（或環境變數 CARGO_TARGET_DIR，預設為 repo_root / "target"）
       於 debug 或 release 子目錄尋找 serialwrapd 與 serialwrap。
    若檔案不存在則拋出 AssertionError。
    """
    if caller_daemon and caller_cli and not str(caller_daemon).endswith(".py") and not str(caller_cli).endswith(".py"):
        d_path = Path(caller_daemon)
        cli_path = Path(caller_cli)
        assert d_path.is_file(), f"呼叫端指定的 Rust daemon 二進位不存在：{d_path}"
        assert cli_path.is_file(), f"呼叫端指定的 Rust CLI 二進位不存在：{cli_path}"
        return d_path, cli_path

    target_root = Path(cargo_target_dir or os.environ.get("CARGO_TARGET_DIR") or (repo_root / "target"))
    candidates = [target_root / "debug", target_root / "release"]
    for cand in candidates:
        d = cand / "serialwrapd"
        c = cand / "serialwrap"
        if d.is_file() and c.is_file():
            return d, c

    searched = ", ".join(str(c) for c in candidates)
    raise AssertionError(
        f"未找到 Rust 二進位檔（已搜尋：{searched}）。"
        f"請先 cargo build -p sw-daemon -p sw-cli，或明確指定 SERIALWRAP_DAEMON_EXE 與 SERIALWRAP_CLI_EXE。"
    )


def assert_no_wal_tx_leak(wal_records: list[dict], marker: str | bytes) -> None:
    """斷言 WAL 記錄中未外洩禁止的命令 marker（OD-81／DF-27／P8-a190a57-2）。

    同時檢查：
    1. 各記錄之明文 metadata（排除 payload_b64）。
    2. 解碼 payload_b64 之個別記錄內容。
    3. 依序串接所有 TX 方向記錄的 payload（防止跨記錄切分）。
    4. 依序串接全部記錄的 payload。
    """
    marker_bytes = marker.encode("utf-8") if isinstance(marker, str) else marker
    marker_str = marker if isinstance(marker, str) else marker.decode("utf-8", errors="replace")

    tx_stream = bytearray()
    all_stream = bytearray()

    for rec in wal_records:
        rec_meta = {k: v for k, v in rec.items() if k != "payload_b64"}
        assert marker_str not in str(rec_meta), f"命令標記外洩至 WAL metadata：{rec_meta}"

        b64 = rec.get("payload_b64")
        if not b64 or not isinstance(b64, str):
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue

        assert marker_bytes not in raw, (
            f"命令標記外洩至單筆 WAL payload（seq={rec.get('seq')}, source={rec.get('source')}）"
        )

        all_stream.extend(raw)
        if rec.get("dir") == "TX":
            tx_stream.extend(raw)

    assert marker_bytes not in tx_stream, "命令標記外洩至 WAL TX 串接資料流"
    assert marker_bytes not in all_stream, "命令標記外洩至 WAL 全體串接資料流"


@contextmanager
def _select_engine(flavor: str):
    """明確選擇受測 CLI／daemon 之 artifact，離開時完整還原（OD-81／DF-27／P8-a190a57-1）。"""
    old_ab = dict(ab_env.AB_ENV)
    old_env = {k: os.environ.get(k) for k in ab_env.AB_KEYS}
    try:
        if flavor == "python":
            for k in ("SERIALWRAP_DAEMON_EXE", "SERIALWRAP_CLI_EXE"):
                ab_env.AB_ENV.pop(k, None)
                os.environ.pop(k, None)
            ab_env.AB_ENV["SERIALWRAP_AB_LABEL"] = "python-baseline"
            os.environ["SERIALWRAP_AB_LABEL"] = "python-baseline"
            expected_exe = ab_env.expected_daemon_exe()
            yield expected_exe
        elif flavor == "rust":
            caller_d = old_ab.get("SERIALWRAP_DAEMON_EXE") or old_env.get("SERIALWRAP_DAEMON_EXE")
            caller_cli = old_ab.get("SERIALWRAP_CLI_EXE") or old_env.get("SERIALWRAP_CLI_EXE")
            rust_d, rust_cli = resolve_rust_artifacts(
                caller_daemon=caller_d,
                caller_cli=caller_cli,
            )
            ab_env.AB_ENV["SERIALWRAP_DAEMON_EXE"] = str(rust_d)
            ab_env.AB_ENV["SERIALWRAP_CLI_EXE"] = str(rust_cli)
            ab_env.AB_ENV["SERIALWRAP_AB_LABEL"] = "rust-p8"
            os.environ["SERIALWRAP_DAEMON_EXE"] = str(rust_d)
            os.environ["SERIALWRAP_CLI_EXE"] = str(rust_cli)
            os.environ["SERIALWRAP_AB_LABEL"] = "rust-p8"
            expected_exe = ab_env.expected_daemon_exe()
            yield expected_exe
        else:
            raise ValueError(f"未知引擎規格：{flavor}")
    finally:
        ab_env.AB_ENV.clear()
        ab_env.AB_ENV.update(old_ab)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _df27_post_login_timeout_scenario(flavor: str, tmp_path: Path, expected_daemon_exe: str | None = None):
    """DF-27 黑箱情境：登入後 post_login_cmd ('sh') 回應非 prompt 文字導致 POST_LOGIN_CMD_TIMEOUT。

    以短 timeout_s（2 s）觸發逾時進 DETACHED，拒絕期間送唯一 marker 之 cmd submit 驗證零 TX，
    並於逾時後恢復 FakeTarget 之 sh 回應。
    yield (rig, tgt, s_detached)。
    """
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="# ",
        password="pw-df27",
        delay_ms=0,
        login_steps=[
            {"expect": "", "respond": "\r\nLogin: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": "pw-df27", "respond": "\r\nCM> "},
        ],
        commands={"overrides": {"sh": {"response": "\r\n[subshell waiting]\r\n"}}},
    )
    rig = P7Rig(tmp_path / "p7")
    try:
        rig.start("bcm", [tgt], timeout_s=2)
        assert rig.daemon is not None and rig.daemon.proc is not None
        assert_daemon_artifact(rig.daemon.proc.pid)
        if expected_daemon_exe:
            actual_exe = os.path.realpath(os.readlink(f"/proc/{rig.daemon.proc.pid}/exe"))
            assert actual_exe == os.path.realpath(expected_daemon_exe), (
                f"受測 daemon 二進位與預期不符：期望 {expected_daemon_exe}，實際 {actual_exe}"
            )

        # 1. 登入成功後送出 sh，假 target 回傳非 prompt 文字，等待 timeout_s 逾時
        s_detached = wait_state(rig.env, "COM0", "DETACHED", timeout_s=15.0)
        last_err = s_detached.get("last_error")
        assert last_err == "POST_LOGIN_CMD_TIMEOUT", (
            f"必須觀察到精確的 POST_LOGIN_CMD_TIMEOUT，實際為 {last_err!r}；"
            f"若為 LOGIN_REQUIRED、TRANSPORT_STALL 或 RX_FLOOD 則判定 fixture 不成立"
        )

        # 2. 觀察到逾時後，讓假 target 後續 sh 恢復正常回應，不呼叫手動 attach／recover、不重新插拔
        tgt.overrides["sh"] = {"response": "\r\n# "}

        # 3. 拒絕期間送唯一識別字串的 command.submit（cmd submit）
        marker = f"DF27_ZERO_TX_{flavor.upper()}_{int(time.time() * 1000)}"
        rc, res = rig.cli("cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}")
        assert rc != 0, f"拒絕期間 cmd submit 應失敗，但 rc={rc}：{res}"
        assert res is not None and res.get("ok") is False, res
        assert res.get("error_code") == "SESSION_NOT_READY", res

        # 4. 以假 DUT 收到的 bytes 及 WAL 證明該請求零 TX（不將系統登入/重探 TX 誤算為違規）
        dut_bytes = tgt.rx_log()
        assert marker.encode("utf-8") not in dut_bytes, "拒絕期間的命令文字外洩至 DUT RX log"
        wal_records = rig.wal_records("COM0")
        assert_no_wal_tx_leak(wal_records, marker)

        yield rig, tgt, s_detached
    finally:
        evidence_dir = ROOT / "target" / "s2-par" / "wp71" / "item2"
        if evidence_dir.is_dir():
            try:
                dut_log = evidence_dir / f"{flavor}_dut_rx.log"
                dut_log.write_bytes(bytes(tgt.rx_log()))
                wal_file = evidence_dir / f"{flavor}_wal.json"
                wal_file.write_text(
                    json.dumps(rig.wal_records("COM0"), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        rig.stop()


def test_df27_python_post_login_timeout_stays_detached(tmp_path):
    """OD-81／DF-27 Python 基線：POST_LOGIN_CMD_TIMEOUT 後維持 DETACHED，不自動重探。

    在涵蓋 RX idle 與既有多段退避的觀察窗內（40 秒），持續 DETACHED、錯誤不變、沒有新增自動登入 TX。
    """
    with _select_engine("python") as exp_exe:
        with _df27_post_login_timeout_scenario("python", tmp_path, expected_daemon_exe=exp_exe) as (rig, tgt, _):
            tx_before = tgt.rx_log()
            deadline = time.monotonic() + 40.0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                curr = _session_row(rig.env, "COM0")
                assert curr is not None, "session COM0 遺失"
                assert curr.get("state") == "DETACHED", (
                    f"Python 預期維持 DETACHED，但變更為 {curr.get('state')!r}"
                )
                assert curr.get("last_error") == "POST_LOGIN_CMD_TIMEOUT", curr.get("last_error")
            tx_after = tgt.rx_log()
            assert tx_after == tx_before, "Python 在觀察窗內不應有新增自動登入／重探 TX"


def test_df27_rust_post_login_timeout_reprobes_ready(tmp_path):
    """OD-81／DF-27 Rust 重構：POST_LOGIN_CMD_TIMEOUT 後依退避規則自動重探並回到 READY。

    重探完成後清除錯誤（last_error is None）並完成新的 ready_probe nonce 確認。
    未修 Rust 時因尚未納入 reprobe 錯誤碼，本測試預期在 wait_state(READY) 逾時而紅燈。
    """
    with _select_engine("rust") as exp_exe:
        with _df27_post_login_timeout_scenario("rust", tmp_path, expected_daemon_exe=exp_exe) as (rig, tgt, _):
            s_ready = wait_state(rig.env, "COM0", "READY", timeout_s=40.0)
            assert s_ready.get("last_error") is None, s_ready
            sent = tgt.rx_log()
            assert b"echo __READY__" in sent, "Rust 應完成新的 ready_probe nonce 確認"


def test_df27_rust_artifact_selector_resolution(tmp_path, monkeypatch):
    """P8-a190a57-1 / P8-a81ff33-1 自測：驗證 resolve_rust_artifacts 與 _select_engine 解析路徑。

    1. 呼叫端明確指定且非 .py 之二進位時，優先保留該自訂路徑，不被覆寫。
    2. 未指定時，若給定 cargo_target_dir，則解析至該目錄下的 debug 二進位。
    3. 未指定且無 cargo_target_dir 時，依假 repo_root 解析至 fake_repo/target/debug（monkeypatch 隔離 CARGO_TARGET_DIR）。
    4. 設定環境變數 CARGO_TARGET_DIR 時，優先解析至該目錄下的二進位。
    5. 呼叫端指定不存在之路徑時，明確拋出 AssertionError。
    6. 搜尋目錄皆無二進位時，拋出明確之 AssertionError。
    """
    # 建立假二進位供自測
    custom_dir = tmp_path / "custom_bin"
    custom_dir.mkdir()
    c_d = custom_dir / "custom_serialwrapd"
    c_cli = custom_dir / "custom_serialwrap"
    c_d.touch(mode=0o755)
    c_cli.touch(mode=0o755)

    # 1. 呼叫端明確指定存在且非 .py 之二進位
    d, cli = resolve_rust_artifacts(caller_daemon=str(c_d), caller_cli=str(c_cli))
    assert d == c_d
    assert cli == c_cli

    # 驗證 _select_engine("rust") 保留呼叫端指定路徑
    old_ab = dict(ab_env.AB_ENV)
    try:
        ab_env.AB_ENV["SERIALWRAP_DAEMON_EXE"] = str(c_d)
        ab_env.AB_ENV["SERIALWRAP_CLI_EXE"] = str(c_cli)
        with _select_engine("rust") as exp:
            assert exp == os.path.realpath(str(c_d))
            assert ab_env.AB_ENV["SERIALWRAP_DAEMON_EXE"] == str(c_d)
            assert ab_env.AB_ENV["SERIALWRAP_CLI_EXE"] == str(c_cli)
    finally:
        ab_env.AB_ENV.clear()
        ab_env.AB_ENV.update(old_ab)

    # 2. 依 cargo_target_dir 解析
    cargo_target = tmp_path / "cargo_target"
    (cargo_target / "debug").mkdir(parents=True)
    ct_d = cargo_target / "debug" / "serialwrapd"
    ct_cli = cargo_target / "debug" / "serialwrap"
    ct_d.touch(mode=0o755)
    ct_cli.touch(mode=0o755)
    d2, cli2 = resolve_rust_artifacts(cargo_target_dir=cargo_target)
    assert d2 == ct_d
    assert cli2 == ct_cli

    # 3. 預設解析至假 repo_root 的 target/debug（隔離環境變數 CARGO_TARGET_DIR）
    fake_repo = tmp_path / "fake_repo"
    fake_target_debug = fake_repo / "target" / "debug"
    fake_target_debug.mkdir(parents=True)
    ft_d = fake_target_debug / "serialwrapd"
    ft_cli = fake_target_debug / "serialwrap"
    ft_d.touch(mode=0o755)
    ft_cli.touch(mode=0o755)

    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    d3, cli3 = resolve_rust_artifacts(repo_root=fake_repo)
    assert d3 == ft_d
    assert cli3 == ft_cli

    # 4. 驗證環境變數 CARGO_TARGET_DIR 分支
    env_target = tmp_path / "env_target"
    (env_target / "debug").mkdir(parents=True)
    et_d = env_target / "debug" / "serialwrapd"
    et_cli = env_target / "debug" / "serialwrap"
    et_d.touch(mode=0o755)
    et_cli.touch(mode=0o755)

    monkeypatch.setenv("CARGO_TARGET_DIR", str(env_target))
    d_env, cli_env = resolve_rust_artifacts(repo_root=fake_repo)
    assert d_env == et_d
    assert cli_env == et_cli

    # 5. 指定不存在的路徑時報錯
    non_existent = tmp_path / "non_existent"
    with pytest.raises(AssertionError, match="呼叫端指定的 Rust daemon 二進位不存在"):
        resolve_rust_artifacts(caller_daemon=str(non_existent), caller_cli=str(c_cli))

    # 6. 搜尋目錄無二進位時報錯
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    with pytest.raises(AssertionError, match="未找到 Rust 二進位檔"):
        resolve_rust_artifacts(repo_root=empty_repo)


def test_df27_wal_zero_tx_assertion_negative_test():
    """P8-a190a57-2 負向自測：驗證 assert_no_wal_tx_leak 能攔截 base64 編碼及跨記錄切分之 payload。"""
    marker = "DF27_ZERO_TX_TEST_SECRET_98765"

    # 1. 舊式 str(rec) 檢查會漏掉 base64 payload，但 assert_no_wal_tx_leak 必定攔截（拋出 AssertionError）
    b64_payload = base64.b64encode(f"cmd: echo {marker}\n".encode("utf-8")).decode("ascii")
    leaked_record = {
        "seq": 10,
        "dir": "TX",
        "source": "agent:test",
        "cmd_id": "c1",
        "payload_b64": b64_payload,
    }

    # 證明舊式檢查在此負向樣本上會漏掉
    old_check_missed = not any(marker in str(rec) for rec in [leaked_record])
    assert old_check_missed, "證明舊式 str(rec) 檢查確實漏掉了 base64 編碼的禁止命令"

    # 證明 assert_no_wal_tx_leak 必定攔截單筆 payload 外洩
    with pytest.raises(AssertionError, match="命令標記外洩至單筆 WAL payload"):
        assert_no_wal_tx_leak([leaked_record], marker)

    # 2. 跨記錄切分測試：前半段在 rec1、後半段在 rec2（正式 WAL 欄位 dir=TX, source=agent:test）
    half1 = marker[:15]
    half2 = marker[15:]
    rec1 = {
        "seq": 1,
        "dir": "TX",
        "source": "agent:test",
        "payload_b64": base64.b64encode(half1.encode("utf-8")).decode("ascii"),
    }
    rec2 = {
        "seq": 2,
        "dir": "TX",
        "source": "agent:test",
        "payload_b64": base64.b64encode(half2.encode("utf-8")).decode("ascii"),
    }

    # 單筆檢查均不含完整 marker
    assert marker.encode("utf-8") not in base64.b64decode(rec1["payload_b64"])
    assert marker.encode("utf-8") not in base64.b64decode(rec2["payload_b64"])

    # 證明串接檢查能攔截跨記錄切分
    with pytest.raises(AssertionError, match="命令標記外洩至 WAL TX 串接資料流"):
        assert_no_wal_tx_leak([rec1, rec2], marker)

    # 3. 審查實測案例（P8-a190a57-2）：TX / RX / TX 交錯跨記錄切分
    # 依序輸入三筆記錄：
    # 1. dir=TX, source=agent:test：DF27_ZERO_
    # 2. dir=RX, source=device：background noise\n
    # 3. dir=TX, source=agent:test：TX_RUST_123
    # 預期攔截完整 TX marker DF27_ZERO_TX_RUST_123。
    # 舊 helper 以 source 含 TX 判定，因 source=agent:test 漏掉 TX 串接；且 all_stream 被 RX 雜訊切斷，因而漏判。
    interleaved_marker = "DF27_ZERO_TX_RUST_123"
    rec_tx1 = {
        "seq": 11,
        "dir": "TX",
        "source": "agent:test",
        "payload_b64": base64.b64encode(b"DF27_ZERO_").decode("ascii"),
    }
    rec_rx = {
        "seq": 12,
        "dir": "RX",
        "source": "device",
        "payload_b64": base64.b64encode(b"background noise\n").decode("ascii"),
    }
    rec_tx2 = {
        "seq": 13,
        "dir": "TX",
        "source": "agent:test",
        "payload_b64": base64.b64encode(b"TX_RUST_123").decode("ascii"),
    }
    with pytest.raises(AssertionError, match="命令標記外洩至 WAL TX 串接資料流"):
        assert_no_wal_tx_leak([rec_tx1, rec_rx, rec_tx2], interleaved_marker)

    # 4. 正常記錄通過檢查
    clean_rec = {
        "seq": 14,
        "dir": "TX",
        "source": "agent:test",
        "payload_b64": base64.b64encode(b"normal probe\n").decode("ascii"),
    }
    assert_no_wal_tx_leak([clean_rec], marker)


def test_login_with_delay_and_noise(p7, tmp_path):
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="# ",
        password="pw-noise",
        delay_ms=800,
        noise={"interval_ms": 50, "pattern": "\r\n[  12.345678] kernel: usb 1-1: noise"},
        login_steps=[
            {"expect": "", "respond": "\r\nLogin: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": "pw-noise", "respond": "\r\nCM> "},
        ],
        commands={"overrides": {"sh": {"response": "\r\n# "}}},
    )
    p7.start("bcm", [tgt], timeout_s=60)                       # OD-81：單案給予 60s 充裕 timeout，不依賴逾時重試
    # 延遲與雜訊下給予充足 profile timeout_s 確保各步驟完成；輪詢上限取 180 s，斷言仍為最終 READY。
    s = wait_state(p7.env, "COM0", "READY", timeout_s=180.0)
    assert s["last_error"] is None, s
    sent = tgt.rx_log()
    assert b"root\n" in sent and b"pw-noise\n" in sent and b"sh\n" in sent and b"echo __READY__" in sent, sent


def test_control_latency_under_flood_with_events(tmp_path):
    """RX 洪水＋ALL 規則下的控制延遲（OD-59：event_latency flood 規則）。

    預設 pytest／CI 使用 200 筆 command_cancel、20 個 stop_request daemon、各 1 輪，僅作快速檢查；不代表達成 OD-59
    契約。P7 gate 專屬列以 resource_limits.yaml 的 samples_per_run 與 repeats 執行兩事件，並要求兩者逐字符合契約。
    command_cancel 每輪共用 daemon 且整輪洪水，stop_request 每筆使用新 daemon 並於送出前暖機 stop_warmup_s。每輪由
    `judge_run` 判定——有效樣本最大值 > `max_ms_flood` 即 FAIL；錯誤、缺樣、負載無效即證據不足（不得 PASS）；
    loadavg 峰值 > CPU 數的輪次作廢重跑，重跑仍作廢即證據不足。
    另斷言洪水期間規則確有比對（match_recorded ≥ 1），且 `event_dropped` 出現、筆數 ≤ 送出的洪水行數、皆屬 COM0。"""
    tool = _measure_tool()
    harness = tool._import_harness()
    contract = tool.load_contract()
    assert contract.get("gate_rule") == "every_run_max_le_max_ms", contract.get("gate_rule")
    contract_samples = int(contract["samples_per_run"])
    contract_repeats = int(contract["repeats"])
    if REQUIRE_OD59_CONTRACT:
        assert (CANCEL_SAMPLES, STOP_SAMPLES, OD59_REPEATS) == (contract_samples, contract_samples, contract_repeats), {
            "configured": (CANCEL_SAMPLES, STOP_SAMPLES, OD59_REPEATS),
            "contract": (contract_samples, contract_repeats),
        }
    rows = {str(e.get("event")): e for e in contract.get("events") or []}
    flood_cfg = contract["flood_load"]
    verdicts: dict[str, list[dict]] = {"command_cancel": [], "stop_request": []}
    event_rows: list[dict] = []
    for repeat in range(OD59_REPEATS):
        for event in ("command_cancel", "stop_request"):
            assert rows[event].get("measure") == "external", rows[event]
            limit = rows[event]["max_ms_flood"]
            for attempt in range(VOID_RETRIES + 1):
                root = tmp_path / f"{event}-r{repeat + 1}-{attempt}"
                if event == "command_cancel":
                    run, events = _cancel_run(tool, harness, root, limit, flood_cfg)
                else:
                    run = _stop_run(tool, harness, root, limit, flood_cfg)
                    events = None
                if not (run["void"] and attempt < VOID_RETRIES):
                    break
                time.sleep(5.0)
            verdicts[event].append(run)
            if events is not None:
                event_rows.append({"repeat": repeat + 1, **events})
    summary = {ev: [{k: r.get(k) for k in ("verdict", "max_ms", "p99_ms", "count", "limit_ms", "void", "errors",
                                           "load_problems", "host_load")} for r in runs]
               for ev, runs in verdicts.items()}
    full_contract = REQUIRE_OD59_CONTRACT and (CANCEL_SAMPLES, STOP_SAMPLES, OD59_REPEATS) == (
        contract_samples, contract_samples, contract_repeats)
    evidence_class = "OD-59 契約量測" if full_contract else "quick_check_only"
    print("control_latency_summary", json.dumps({
        "evidence_class": evidence_class,
        "sample_plan": {"command_cancel_per_run": CANCEL_SAMPLES, "stop_request_per_run": STOP_SAMPLES,
                        "repeats": OD59_REPEATS},
        "runs": summary,
        "events": event_rows,
    }, ensure_ascii=False, sort_keys=True))
    assert all(run["verdict"] == "PASS" for runs in verdicts.values() for run in runs), summary
    assert len(event_rows) == OD59_REPEATS, event_rows
    assert all(events["matched"] >= 1 for events in event_rows), event_rows
    assert all(1 <= events["dropped"] <= events["lines_sent"] for events in event_rows), event_rows
    assert all(events["dropped_selectors"] == ["COM0"] for events in event_rows), event_rows
