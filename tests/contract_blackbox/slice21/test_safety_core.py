"""Slice 2.1 安全出口（L1 部分）黑箱案例（P5-T20）。

- `shell_daemon`：COM0 已 READY 的 daemon（Task 15 harness）。
- `cred_daemon`／`banner_target`／`profile_variant`：見 ./conftest.py。
- 拒絕路徑一律以 `t.written()` 與 WAL TX 行零增量判定（§11.3 不變量 1）。
"""
import errno
import json
import os
import stat
import time

import pytest

pytestmark = pytest.mark.cap_posix_pty

UBOOT_BANNER = b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n"
PASSTHROUGH_QUIET_S = 30.0


# ---------------------------------------------------------------------------
# 共用輔助
# ---------------------------------------------------------------------------


def _session(d, com: str = "COM0") -> dict:
    return d.rpc("session.get_state", {"selector": com})["session"]


def _wait_last_error(d, want: str, com: str = "COM0", timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = _session(d, com)
        if s.get("last_error") == want:
            return s
        if time.monotonic() > deadline:
            raise AssertionError(f"逾時未見 last_error={want}：{s}")
        time.sleep(0.2)


def _tx_count(d) -> int:
    return sum(1 for r in d.wal_records() if r.get("dir") == "TX")


def _tgt(d):
    return d.targets["COM0"]


def _submit(d, cmd: str, *extra: str) -> dict:
    return d.sw("cmd", "submit", "--selector", "COM0", "--cmd", cmd, *extra)


# ---------------------------------------------------------------------------
# 規格原文案例
# ---------------------------------------------------------------------------


def test_zero_tx_while_autoboot_quiet(shell_daemon):
    t = shell_daemon.targets["COM0"]
    t.emit(b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n")
    time.sleep(0.5)
    before = len(t.written())
    r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo should-not-send")
    assert r["_rc"] == 2 and r["error_code"] == "AUTOBOOT_QUIET" and 179.0 <= r["retry_after_s"] <= 180.0
    time.sleep(1.0)
    assert t.written()[before:] == b""


def test_external_open_blocked_then_release_attach_handoff(shell_daemon):
    if os.geteuid() == 0:
        pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
    slave = shell_daemon.targets["COM0"].slave_path
    with pytest.raises(OSError) as e:
        os.open(slave, os.O_RDWR | os.O_NOCTTY)
    assert e.value.errno == errno.EBUSY                                     # BC-04 TIOCEXCL
    rel = shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    assert rel["ok"] is True and rel["session"]["state"] == "RELEASED"
    fd = os.open(slave, os.O_RDWR | os.O_NOCTTY)                             # 外部工具接手；FakeTarget 持有 master，daemon 關閉前漏 TIOCNXCL 時此處 EBUSY（R0-F3）
    held = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert held["error_code"] == "DEVICE_STILL_HELD" and os.getpid() in held["pids"] and held["holder_unknown"] is False
    before = len(shell_daemon.targets["COM0"].written())
    forced = shell_daemon.sw("device", "attach", "--selector", "COM0", "--force")   # §6.2：force 不得略過已識別外部 holder
    assert forced["_rc"] == 2 and forced["error_code"] == "DEVICE_STILL_HELD"
    assert forced["pids"] == [os.getpid()] and forced["holder_unknown"] is False
    shell_daemon.wait_state("COM0", "RELEASED", timeout=1.0)              # 被拒後 released 保護不得被清除
    time.sleep(1.0)
    assert shell_daemon.targets["COM0"].written()[before:] == b"", "被拒的 force attach 不得對 UART 送出任何 byte"
    os.close(fd)
    ok = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert ok["ok"] is True
    shell_daemon.wait_state("COM0", "READY")


# ---------------------------------------------------------------------------
# 憑證、釋放與 socket 權限
# ---------------------------------------------------------------------------


def test_credentials_unresolved_no_empty_login(cred_daemon):
    s = _wait_last_error(cred_daemon, "CREDENTIALS_UNRESOLVED")
    assert s["state"] == "ATTACHED"
    assert s["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert cred_daemon.targets["COM0"].written() == b""


def test_released_survives_two_restarts_dynamic(shell_daemon, slice21_daemon_factory):
    t = shell_daemon.targets["COM0"]
    rel = shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    assert rel["ok"] is True
    shell_daemon.wait_state("COM0", "RELEASED", timeout=10)
    d = shell_daemon
    for _ in range(2):
        assert d.stop() == 0
        d = slice21_daemon_factory(shell_daemon.root, reuse_dirs=True)
        d.wait_state("COM0", "RELEASED", timeout=10)
        fd = os.open(t.slave_path, os.O_RDWR | os.O_NOCTTY)                 # 重啟後 daemon 未持有 tty
        os.close(fd)


def test_socket_mode_0660(shell_daemon):
    assert "SERIALWRAP_SOCKET_GROUP" not in shell_daemon.env
    assert stat.S_IMODE(os.stat(shell_daemon.endpoint).st_mode) == 0o660


def test_attach_not_ready_last_error_field(cred_daemon):
    t = cred_daemon.targets["COM0"]
    _wait_last_error(cred_daemon, "CREDENTIALS_UNRESOLVED")
    r = cred_daemon.sw("session", "attach", "--selector", "COM0")
    assert r["ok"] is False
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert "last_error_detail" in r
    rec = cred_daemon.sw("session", "recover", "--selector", "COM0")
    assert rec["ok"] is True
    assert rec["recovered"] is False
    assert rec["error_code"] == "SESSION_NOT_READY"
    assert rec["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert t.written() == b""


# ---------------------------------------------------------------------------
# Passthrough 安靜升 READY（SC-35）
# ---------------------------------------------------------------------------


def test_passthrough_quiet_ready_and_cmd(slice21_daemon_factory, profile_variant, tmp_path):
    profile = profile_variant(platform="passthrough", prompt_regex="'.*'", ready_probe="''")
    d = slice21_daemon_factory(tmp_path / "passthrough", profile_yaml=profile)
    t = d.targets["COM0"]
    d.wait_state("COM0", "ATTACHED", timeout=15)
    t0 = time.monotonic()
    assert t.written() == b""
    time.sleep(max(0.0, t0 + 29.0 - time.monotonic()))
    early = _submit(d, "echo early")
    assert early["error_code"] == "SESSION_NOT_READY"
    assert 0 <= early["passthrough_ready_in_s"] <= 1
    time.sleep(max(0.0, t0 + PASSTHROUGH_QUIET_S + 1.0 - time.monotonic()))
    assert _session(d)["state"] == "READY"
    rc = d.sw(
        "cmd", "submit", "--selector", "COM0", "--cmd", "echo __RC__=0",
        "--completion-regex", r"(?m)^__RC__=0\r*$",
    )
    done = d.wait_command_done(rc["cmd_id"], timeout=30)
    assert done["partial"] is False
    to = _submit(d, "sleep 20", "--cmd-timeout", "2")
    done2 = d.wait_command_done(to["cmd_id"], timeout=30)
    assert done2["error_code"] == "PROMPT_TIMEOUT"
    assert done2["recovery_action"] == "NONE"
    written = t.written()
    assert b"\x03" not in written and b"\x04" not in written, "passthrough 逾時不得送 CTRL_C／CTRL_D"
    assert _session(d)["state"] == "READY"


# ---------------------------------------------------------------------------
# WAL 自癒、suspended 狀態與拒絕路徑
# ---------------------------------------------------------------------------


def test_wal_unlink_recreates_with_meta(shell_daemon):
    wal = shell_daemon.wal_dir / "raw.wal.ndjson"
    wal.unlink()
    sub = _submit(shell_daemon, "echo after-unlink")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
    recs = shell_daemon.wal_records()
    assert recs[0]["dir"] == "META"
    m = recs[0]["meta"] if "meta" in recs[0] else recs[0]
    assert m["event"] == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"
    assert m.get("loss_flag", recs[0].get("loss_flag")) is True
    assert m["potentially_lost_seq_range"][1] < recs[1]["seq"]


def _payload(rec: dict) -> bytes:
    import base64

    return base64.b64decode(rec.get("payload_b64") or "")


def wal_unlink_evidence_gaps(old_recs: list[dict], new_recs: list[dict], *, rx_markers: list[bytes],
                             tx_markers: list[bytes], unlink_mono_ns: int) -> list[str]:
    """OD-46：外部 unlink 期間持續 RX／TX 時，「舊 inode（unlink 前開啟的 fd 讀回）＋重建後現行檔」的 WAL 證據不得漏。

    回傳缺口清單（空 list 才算通過）：
    - 重建檔第一筆為 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` META（loss_flag true），`potentially_lost_seq_range`
      恰為舊 inode 的 [首 seq, 末 seq]，且重建 META 的 seq 緊接舊 inode 末 seq；
    - 兩檔合併後 seq 自首筆起逐一連續、無重複（unlink 與偵測之間寫入舊 inode 的紀錄也在）；
    - 每個 DUT 送出的 RX 標記與每個 agent 命令的 TX 標記在合併 WAL 中恰出現一次（RX 依送出順序）；
    - 流量確實橫跨 unlink：unlink 之前舊 inode、重建 META 之後新檔，皆有 RX 與 TX 標記。
    """
    gaps: list[str] = []
    if not old_recs:
        gaps.append("舊 inode 無紀錄（unlink 前未持有 fd 或檔案為空）")
    if not new_recs:
        return gaps + ["unlink 後未重建現行 WAL"]
    head = new_recs[0]
    meta = head.get("meta") if isinstance(head.get("meta"), dict) else head
    if head.get("dir") != "META" or meta.get("event") != "WAL_RECREATED_AFTER_EXTERNAL_UNLINK":
        gaps.append(f"重建檔第一筆不是 WAL_RECREATED_AFTER_EXTERNAL_UNLINK META：{head}")
    elif meta.get("loss_flag", head.get("loss_flag")) is not True:
        gaps.append(f"重建 META 未標 loss_flag：{head}")
    if old_recs and head.get("dir") == "META":
        lost = meta.get("potentially_lost_seq_range")
        want = [old_recs[0]["seq"], old_recs[-1]["seq"]]
        if lost != want:
            gaps.append(f"potentially_lost_seq_range={lost!r}，舊 inode 實際 seq 範圍 {want}")
        if head.get("seq") != old_recs[-1]["seq"] + 1:
            gaps.append(f"重建 META seq={head.get('seq')} 未緊接舊 inode 末 seq {old_recs[-1]['seq']}")
    seqs = [r.get("seq") for r in old_recs + new_recs]
    if seqs and seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        missing = sorted(set(range(min(seqs), max(seqs) + 1)) - set(seqs))
        dup = sorted({x for x in seqs if seqs.count(x) > 1})
        gaps.append(f"合併 WAL seq 不連續：缺 {missing[:10]}、重複 {dup[:10]}")
    def _stream(recs: list[dict], direction: str) -> bytes:
        return b"".join(_payload(r) for r in recs if r.get("dir") == direction and r.get("com") == "COM0")
    all_rx, all_tx = _stream(old_recs + new_recs, "RX"), _stream(old_recs + new_recs, "TX")
    pos = 0
    for m in rx_markers:
        n = all_rx.count(m)
        if n != 1:
            gaps.append(f"RX 標記 {m!r} 在合併 WAL 出現 {n} 次（須恰 1 次）")
            continue
        at = all_rx.find(m)
        if at < pos:
            gaps.append(f"RX 標記 {m!r} 順序與 DUT 送出順序不符")
        pos = at
    for m in tx_markers:
        n = all_tx.count(m)
        if n != 1:
            gaps.append(f"TX 標記 {m!r} 在合併 WAL 出現 {n} 次（須恰 1 次）")
    before = [r for r in old_recs if int(r.get("mono_ts_ns") or 0) < unlink_mono_ns]
    after = new_recs[1:]
    for label, recs in (("unlink 之前（舊 inode）", before), ("重建之後（新檔）", after)):
        rx, tx = _stream(recs, "RX"), _stream(recs, "TX")
        if not any(m in rx for m in rx_markers):
            gaps.append(f"{label}無 RX 標記：流量未橫跨 unlink")
        if not any(m in tx for m in tx_markers):
            gaps.append(f"{label}無 TX 標記：流量未橫跨 unlink")
    return gaps


def test_wal_unlink_under_sustained_traffic_keeps_evidence(shell_daemon):
    """OD-46：`s21-wal-unlink-under-traffic` 移出 l15_ab 後由本案承擔——unlink 期間 DUT 持續送 RX、agent 持續送命令（TX），
    重建後證據不得漏（判定見 `wal_unlink_evidence_gaps`）。舊 inode 以 unlink 前開啟的唯讀 fd 讀回：不建硬連結
    （硬連結會使 Rust 的 `nlink()==0` 外部 unlink 偵測失真），唯讀 fd 不改變 link 數。"""
    import threading

    d = shell_daemon
    tgt = _tgt(d)
    wal = d.wal_dir / "raw.wal.ndjson"
    stop = threading.Event()
    rx_markers: list[bytes] = []
    tx_markers: list[bytes] = []
    errors: list[str] = []

    def _rx_pump() -> None:
        n = 0
        while not stop.is_set():
            m = b"UNLRX%05d" % n
            tgt.emit(m + b"\r\n")
            rx_markers.append(m)
            n += 1
            time.sleep(0.01)

    def _tx_pump() -> None:
        n = 0
        while not stop.is_set():
            cmd = f"echo UNLTX{n:04d}"
            sub = _submit(d, cmd)
            if sub.get("ok") is not True:
                errors.append(f"submit {cmd} 失敗：{sub}")
                return
            try:
                d.wait_command_done(sub["cmd_id"], timeout=15)
            except AssertionError as exc:
                errors.append(str(exc))
                return
            tx_markers.append(f"{cmd}\n".encode())
            n += 1

    held = open(wal, "rb")
    try:
        threads = [threading.Thread(target=_rx_pump, daemon=True), threading.Thread(target=_tx_pump, daemon=True)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 20
        while len(tx_markers) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        unlink_mono_ns = time.monotonic_ns()
        wal.unlink()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if wal.exists() and any(r.get("dir") == "TX" for r in d.wal_records()):
                    break                                  # 已重建且新檔已記到 agent TX
            except ValueError:
                pass                                       # 讀到寫入中的半行

            time.sleep(0.05)
        time.sleep(1.0)                                    # 重建後再維持一段流量
        stop.set()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        time.sleep(0.5)                                    # 等最後的 RX 落盤
        held.seek(0)
        old_recs = [json.loads(line) for line in held.read().decode("utf-8").splitlines() if line.strip()]
    finally:
        held.close()
    new_recs = d.wal_records()
    gaps = wal_unlink_evidence_gaps(old_recs, new_recs, rx_markers=rx_markers, tx_markers=tx_markers,
                                    unlink_mono_ns=unlink_mono_ns)
    assert not gaps, gaps


def _tx_payloads(recs: list[dict]) -> list[bytes]:
    import base64

    return [base64.b64decode(r.get("payload_b64") or "") for r in recs if r.get("dir") == "TX"]


def test_wal_reset_archives_and_next_command_recorded(shell_daemon):
    """OD-40：l15-wal-interference 的 reset 段改由本案承擔——`wal reset` 把現行 WAL 改名歸檔（輪替），
    之後的命令 TX 記於新現行檔、seq 自頭起算；reset 前的紀錄只留在歸檔檔。"""
    d = shell_daemon
    before = _submit(d, "echo wal-before-reset")
    d.wait_command_done(before["cmd_id"], timeout=15)
    r = d.sw("wal", "reset")
    assert r.get("ok") is True, r
    archived = sorted(d.wal_dir.glob("raw.wal.ndjson.*"))
    assert archived, f"reset 須把現行 WAL 改名歸檔：{sorted(p.name for p in d.wal_dir.iterdir())}"
    assert any(b"echo wal-before-reset\n" in p.read_bytes() or b"ZWNobyB3YWwtYmVmb3JlLXJlc2V0Cg" in p.read_bytes()
               for p in archived), "reset 前的 TX 須留在歸檔檔"
    d.wait_state("COM0", "READY")
    after = _submit(d, "echo wal-after-reset")
    d.wait_command_done(after["cmd_id"], timeout=15)
    recs = d.wal_records()
    payloads = _tx_payloads(recs)
    assert b"echo wal-after-reset\n" in payloads, payloads
    assert b"echo wal-before-reset\n" not in payloads, "reset 前的紀錄不得出現在新現行檔"
    assert recs and recs[0]["seq"] < 10, f"reset 後 seq 須自頭起算：{recs[:2]}"


def test_wal_reader_hold_does_not_block_records(shell_daemon):
    """OD-40：l15-wal-interference 的讀取鎖定段——外部持有 WAL 讀取 handle 期間，命令照常完成且 TX 照記
    （Windows 原生執行時才是真正的共用鎖定情境；POSIX 上 reader 不阻擋，仍驗證不漏記）。"""
    d = shell_daemon
    wal = d.wal_dir / "raw.wal.ndjson"
    assert wal.exists()
    with open(wal, "rb"):
        sub = _submit(d, "echo wal-during-hold")
        d.wait_command_done(sub["cmd_id"], timeout=15)
        assert b"echo wal-during-hold\n" in _tx_payloads(d.wal_records())
    after = _submit(d, "echo wal-after-hold")
    d.wait_command_done(after["cmd_id"], timeout=15)
    assert b"echo wal-after-hold\n" in _tx_payloads(d.wal_records())


def _wait_io_error_observed(d, com: str = "COM0", timeout: float = 10.0) -> dict:
    """OD-36 同步點：等到 daemon 已觀察到 FakeTarget 的 I/O error／裝置消失（session 離開 READY）。
    未設此同步點時，stop() 後立即 submit 兩個 daemon 都尚未察覺，案例前置不成立（T20 報告）。"""
    deadline = time.monotonic() + timeout
    while True:
        s = _session(d, com)
        if s.get("state") != "READY":
            return s
        if time.monotonic() > deadline:
            raise AssertionError(f"逾時：daemon 未觀察到 I/O error（仍 READY）：{s}")
        time.sleep(0.02)


def test_recovering_submit_session_recovering(shell_daemon):
    _tgt(shell_daemon).stop()                                              # FakeTarget I/O error
    _wait_io_error_observed(shell_daemon)
    r = _submit(shell_daemon, "echo x")
    assert r.get("ok") is False, "I/O error 瞬間的 submit 絕不可 ok:true"
    assert r.get("error_code") in ("SESSION_RECOVERING", "SESSION_NOT_READY")


def _fill_queue(d) -> None:
    # OD-36：前景命令須不會在填滿佇列前自行結束（Python CLI 逐筆 submit 256 筆可能超過 30 s，佇列會開始排出）。
    first = _submit(d, "sleep 3600", "--cmd-timeout", "3700")
    assert "cmd_id" in first, first
    for i in range(400):
        r = _submit(d, f"echo fill{i}")
        if r.get("error_code") == "SESSION_QUEUE_FULL":
            return
    raise AssertionError("佇列 400 筆仍未出現 SESSION_QUEUE_FULL")


def _prepare_reject(row: str, request):
    """回傳 (daemon, trigger, expected_error_codes)；trigger(d) 回傳被拒的回應 dict。"""
    if row == "CMD_TOO_LONG":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "echo " + "A" * 16380), {"CMD_TOO_LONG"}
    if row == "CMD_CONTAINS_NEWLINE":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "a\nb"), {"CMD_CONTAINS_NEWLINE"}
    if row == "SESSION_QUEUE_FULL":
        d = request.getfixturevalue("shell_daemon")
        _fill_queue(d)
        return d, lambda d: _submit(d, "echo one-more"), {"SESSION_QUEUE_FULL"}
    if row == "SESSION_NOT_READY-DETACHED":
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).link_path.unlink()                                          # 熱拔除：by-id 連結消失
        d.wait_state("COM0", "DETACHED", timeout=15)
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "SESSION_NOT_READY-ATTACHING":
        d = request.getfixturevalue("shell_daemon")
        d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
        d.wait_state("COM0", "RELEASED", timeout=10)
        _tgt(d).pause_responding(15)                                        # 目標不回應 → 停在 attach 過程
        d.sw("device", "attach", "--selector", "COM0")
        # OD-36：attach 合法的登入／ready probe 序列（source=system）可能在拒絕之後才送出（兩個 daemon 時點不同），
        # 零 TX 窗口只計「被拒請求可歸屬的 TX」：被拒命令 payload 不得出現在 wire，且不得有非 system 來源的 TX WAL。
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}, ("attributable", b"echo x")
    if row == "SESSION_NOT_READY-RELEASED":
        d = request.getfixturevalue("shell_daemon")
        d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
        d.wait_state("COM0", "RELEASED", timeout=10)
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "SESSION_RECOVERING":
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).stop()
        _wait_io_error_observed(d)                                          # OD-36 同步點
        return d, lambda d: _submit(d, "echo x"), {"SESSION_RECOVERING", "SESSION_NOT_READY"}
    if row == "SESSION_NOT_READY-PASSTHROUGH":
        factory = request.getfixturevalue("slice21_daemon_factory")
        profile = request.getfixturevalue("profile_variant")(
            platform="passthrough", prompt_regex="'.*'", ready_probe="''"
        )
        d = factory(request.getfixturevalue("tmp_path") / "pt-reject", profile_yaml=profile)
        d.wait_state("COM0", "ATTACHED", timeout=15)                        # ATTACHED 未滿 30 s（SC-35）
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "PROFILE_NOT_COMMAND_CAPABLE":
        factory = request.getfixturevalue("slice21_daemon_factory")
        profile = request.getfixturevalue("profile_variant")(ready_probe="''")
        d = factory(request.getfixturevalue("tmp_path") / "not-capable", profile_yaml=profile)
        d.wait_state("COM0", "ATTACHED", timeout=15)
        return d, lambda d: _submit(d, "echo x"), {"PROFILE_NOT_COMMAND_CAPABLE"}
    if row == "READY_UNCONFIRMED":
        # OD-36：READY_UNCONFIRMED 只在 #162 READY 再確認 pending 逾時（READY_RECONFIRM_MAX_S＝300 s）或逾次
        # （READY_RECONFIRM_MAX_ATTEMPTS）後出現：READY 中收到 boot banner（quiet＋再確認 pending）且目標不回應，
        # 等到 `ready_reconfirm_failed` 落終態才送被拒命令。
        #   DUT 持續吐出非 prompt 的 bootloader 行（RX 不 idle）：兩個 daemon 都不會在期限內送確認 probe，
        #   以純逾時（READY_RECONFIRM_MAX_S）落終態；不依賴 readiness reprobe（Rust 屬 Slice 2.3，OD-34）。
        import threading

        d = request.getfixturevalue("shell_daemon")
        t = _tgt(d)
        stop = threading.Event()

        def _chatter() -> None:
            while not stop.wait(0.5):
                try:
                    t.emit(b"=> \r\n")
                except Exception:  # noqa: BLE001 — target 已關閉即停止
                    return

        t.emit(UBOOT_BANNER)
        th = threading.Thread(target=_chatter, daemon=True)
        th.start()
        try:
            deadline = time.monotonic() + 340.0
            while True:
                s = _session(d)
                if s.get("ready_reconfirm_failed") is True:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError(f"#162 再確認未在 340 s 內落終態：{s}")
                time.sleep(1.0)
        finally:
            stop.set()
            th.join(timeout=5)
        assert s.get("state") == "READY", f"再確認逾時終態應維持 READY：{s}"
        return d, lambda d: _submit(d, "echo x"), {"READY_UNCONFIRMED"}
    if row == "AUTOBOOT_QUIET":
        # OD-36：先 READY 再送 banner（開機即送 banner 的 session 未曾 READY，兩邊皆回 SESSION_NOT_READY）。
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).emit(UBOOT_BANNER)
        deadline = time.monotonic() + 5.0
        while _session(d).get("boot_quiet_remaining_s") is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"banner 後 quiet 未 arm：{_session(d)}")
            time.sleep(0.05)
        return d, lambda d: _submit(d, "echo should-not-send"), {"AUTOBOOT_QUIET"}
    if row == "SESSION_INTERACTIVE_BUSY":
        # OD-36：lease 期間的拒絕屬執行期（submit 先 accepted），斷言命令終態的 error_code。
        d = request.getfixturevalue("shell_daemon")
        lease = d.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent")
        assert "interactive_id" in lease, lease

        def _busy(d):
            sub = _submit(d, "echo x")
            assert "cmd_id" in sub, sub
            return d.wait_command_done(sub["cmd_id"], timeout=15)

        return d, _busy, {"SESSION_INTERACTIVE_BUSY"}
    if row == "INVALID_COMPLETION_REGEX":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "echo x", "--completion-regex", "("), {"INVALID_COMPLETION_REGEX"}
    if row == "METHOD_NOT_FOUND":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: d.rpc("file.push", {"selector": "COM0"}), {"METHOD_NOT_FOUND"}
    raise AssertionError(f"未知拒絕路徑列：{row}")


REJECT_ROWS = [
    "CMD_TOO_LONG",
    "CMD_CONTAINS_NEWLINE",
    "SESSION_QUEUE_FULL",
    "SESSION_NOT_READY-DETACHED",
    "SESSION_NOT_READY-ATTACHING",
    "SESSION_NOT_READY-RELEASED",
    "SESSION_RECOVERING",
    "SESSION_NOT_READY-PASSTHROUGH",
    "PROFILE_NOT_COMMAND_CAPABLE",
    "READY_UNCONFIRMED",
    "AUTOBOOT_QUIET",
    "SESSION_INTERACTIVE_BUSY",
    "INVALID_COMPLETION_REGEX",
    "METHOD_NOT_FOUND",
]


@pytest.mark.parametrize("row", REJECT_ROWS, ids=REJECT_ROWS)
def reject_tx_clean(scope, new_wire: bytes, new_tx_rows: list[dict]) -> bool:
    """拒絕路徑零 TX 判定：預設 wire 與 TX WAL 皆零增量；`("attributable", payload)` 只排除合法的
    system 來源（attach 登入序列）——被拒 payload 不得上 wire，且不得有非 system 來源的 TX WAL（OD-36）。"""
    if scope is None:
        return new_wire == b"" and not new_tx_rows
    kind, payload = scope
    assert kind == "attributable", scope
    return payload not in new_wire and all(r.get("source") == "system" for r in new_tx_rows)


@pytest.mark.parametrize("row", REJECT_ROWS, ids=REJECT_ROWS)
def test_reject_paths_zero_tx(row, request):
    from ab_rows import gate_row

    gate_row(f"test_reject_paths_zero_tx[{row}]")
    prepared = _prepare_reject(row, request)
    d, trigger, expected = prepared[:3]
    scope = prepared[3] if len(prepared) > 3 else None
    before = len(_tgt(d).written())
    tx0 = _tx_count(d)
    resp = trigger(d)
    assert resp.get("error_code") in expected, f"{row}：回應 {resp}"
    time.sleep(1.0)
    new_wire = _tgt(d).written()[before:]
    new_tx = [r for r in d.wal_records() if r.get("dir") == "TX"][tx0:]
    assert reject_tx_clean(scope, new_wire, new_tx), f"{row}：拒絕路徑不得送出任何 byte／TX WAL：{new_wire!r} {new_tx}"


# ---------------------------------------------------------------------------
# 失效上下文、雙 daemon 競爭
# ---------------------------------------------------------------------------


def test_stale_context_not_replayed(shell_daemon):
    """DF-20（OD-37）：舊 interactive_id 回 INTERACTIVE_NOT_FOUND 且零 TX（Rust 判定；Python 輪逐列標記 DF 差異；
    Rust 實作屬 Slice 2.2 interactive lease，P5 只登記，見 ab_rows.RUST_S21_DEFERRED）。"""
    from ab_rows import gate_row

    gate_row("test_stale_context_not_replayed")
    t = shell_daemon.targets["COM0"]

    def _lease() -> str:
        r = shell_daemon.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent")
        assert "interactive_id" in r, r
        return r["interactive_id"]

    def _assert_stale(old_id: str) -> None:
        r = shell_daemon.sw("session", "interactive-send", "--interactive-id", old_id, "--data", "echo STALE")
        assert r.get("error_code") == "INTERACTIVE_NOT_FOUND", r
        time.sleep(0.5)
        assert b"STALE" not in t.written(), "失效上下文不得被回放到 UART"

    # 1) release → attach（交接後舊 lease 失效）
    old = _lease()
    shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    shell_daemon.wait_state("COM0", "RELEASED", timeout=10)
    shell_daemon.sw("device", "attach", "--selector", "COM0")
    shell_daemon.wait_state("COM0", "READY")
    _assert_stale(old)

    # 2) session recover（重建 bridge 後舊 lease 失效）
    old = _lease()
    shell_daemon.sw("session", "recover", "--selector", "COM0")
    shell_daemon.wait_state("COM0", "READY")
    _assert_stale(old)

    # 3) reboot：U-Boot banner 重新開機後舊 lease 失效
    old = _lease()
    t.emit(UBOOT_BANNER)
    time.sleep(1.0)
    _assert_stale(old)


def test_two_daemons_same_tty(shell_daemon, slice21_daemon_factory, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("EVIDENCE_MISSING: root 下 TIOCEXCL 不生效（§6.2），證據不足")
    t = shell_daemon.targets["COM0"]
    snap = len(t.written())
    # 第二個 daemon：不同 RUN／STATE／socket／lock，BY_ID 目錄指向第一個 daemon 的 FakeTarget
    second = slice21_daemon_factory(
        tmp_path / "second-daemon",
        extra_env={"SERIALWRAP_BY_ID_DIR": shell_daemon.env["SERIALWRAP_BY_ID_DIR"]},
    )
    deadline = time.monotonic() + 20.0
    while True:
        s = _session(second)
        if (s.get("last_error") or "").startswith("ATTACH_FAILED:DeviceHeldError"):
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"第二個 daemon 未回報 ATTACH_FAILED:DeviceHeldError：{s}")
        time.sleep(0.2)
    assert s["state"] != "READY"
    time.sleep(1.0)
    assert t.written()[snap:] == b"", "第二個 daemon 不得對同一 tty 送出任何 TX"


# ---------------------------------------------------------------------------
# 審查 F87（edfc741）：Review Focus 5 的 Python 基線實測（同連線 pipelining＋client 斷線）
# ---------------------------------------------------------------------------


def _recover_tx_payloads(d) -> list[bytes]:
    import base64

    return [
        base64.b64decode(r.get("payload_b64") or "")
        for r in d.wal_records()
        if r.get("dir") == "TX" and "recover" in str(r.get("source") or "")
    ]


@pytest.mark.skipif(os.name != "posix", reason="UDS 寫入對端已關閉的 EPIPE 語意（POSIX）")
def test_pipelined_stop_after_client_gone_not_processed(shell_daemon):
    """同一連線依序送 `session.recover --force` 與 `daemon.stop` 後 client 立即關閉：

    Python `rpc_posix.serve_connection` 先處理 recover（handler 不因斷線取消，救援 TX 照寫），
    其回應 `write`／`drain` 對已關閉的對端失敗（無 except）→ 離開讀取迴圈，緩衝區中的 `daemon.stop` 不再被
    `readline`：daemon 持續運作、socket 保留；之後由新連線 `daemon.stop` 才有序停機（exit 0）。
    """
    import json
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(shell_daemon.endpoint)
    recover = {"id": 3, "method": "session.recover", "params": {"selector": "COM0", "force": True}}
    stop = {"id": 4, "method": "daemon.stop", "params": {}}
    sent_at = time.monotonic()
    sock.sendall((json.dumps(recover) + "\n" + json.dumps(stop) + "\n").encode("utf-8"))
    sock.close()

    # Python 實測（假 DUT 對 Ctrl-C 立即回 prompt）：READY 路徑 `_recover_after_failure` 只送 `system:recover` 的
    # `\x03`（prompt 已回，不再送 `\x04`）。
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not _recover_tx_payloads(shell_daemon):
        time.sleep(0.2)
    time.sleep(1.0)
    assert _recover_tx_payloads(shell_daemon) == [b"\x03"], "recover handler 應照常寫出救援 TX"
    # 超過 force recover 上限（10 s）＋餘裕後再判定：管線中的 stop 若於 handler 結束後才被處理，此處會已退出。
    time.sleep(max(0.0, 11.0 - (time.monotonic() - sent_at)))
    assert shell_daemon.proc.poll() is None, "管線中的 daemon.stop 不得被處理"
    assert os.path.exists(shell_daemon.endpoint), "socket 檔應保留"
    assert shell_daemon.rpc("health.status", {})["ok"] is True
    records = shell_daemon.wal_records()
    assert records, "WAL 應有紀錄"
    assert shell_daemon.stop(timeout=15.0) == 0


@pytest.mark.skipif(os.name != "posix", reason="UDS 端點（POSIX）")
def test_daemon_stop_from_second_connection_during_force_recover(shell_daemon):
    """審查 F87（Opus 修法）：force recover handler 進行中（DUT 暫停回應），由第二條連線送 `daemon.stop`：
    daemon 於 15 s 內 exit 0、救援 TX 已在 WAL、WAL 每一行皆為完整 JSON（停機不在 handler 持有 session 時截斷 WAL）。"""
    import json
    import socket

    shell_daemon.targets["COM0"].pause_responding(8.0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(shell_daemon.endpoint)
    try:
        req = {"id": 3, "method": "session.recover", "params": {"selector": "COM0", "force": True}}
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        sock.close()  # client 斷線（handler 仍在跑）
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _recover_tx_payloads(shell_daemon):
            time.sleep(0.1)
        assert _recover_tx_payloads(shell_daemon), "force recover 應已送出救援 TX"
        started = time.monotonic()
        rc = shell_daemon.stop(timeout=15.0)
        assert rc == 0, (rc, time.monotonic() - started, shell_daemon._stderr_tail(4000))
        assert time.monotonic() - started <= 15.0
    finally:
        sock.close()
    records = shell_daemon.wal_records()  # 任一行不完整即 JSONDecodeError
    assert records
    assert not os.path.exists(shell_daemon.endpoint), "有序停機後 socket 檔應移除"
