//! P8 Task 11：外部工具裝置交接（`device.release`／`device.attach`）與 P8 新功能（file transfer、flash）的整合測試台。
//!
//! 建於 P5 `ActorRig`（真 actor thread＋OpRunner＋TxWriter＋IoWorker）之上，不另起 daemon：
//! - 「零 TX」一律以 FakeDevice 的實際寫出（`dev.tx_len()`／`ShellPeer::tx_len()`）判定，WAL 只作關聯（Review Focus 6）；
//! - drain 卡住以 FakeSerial writer `FakeWriteStep::BlockForever`＋FakeClock 製造（計畫 Task 11 表
//!   `release_drain_stalled_then_completes`；`sw_pal::fakes::serial` 已有此注入點，不改 PAL）；
//! - flash probe 窗口／FLASHING 以 [`super::flash_core::bb`] 的測試替身 coordinator 投遞事件；
//! - file push 前景占用以 [`super::file_transfer_session::XferRig`]＋`ShellPeer` 停在 chunk 迴圈中。

use std::time::{Duration, Instant};

use crossbeam_channel::Receiver;
use sw_core::session_core::SessionRequest;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

use super::file_transfer_session::{push_req, XferRig};
use super::ActorRig;

/// 交接 RPC 的等待上限（真實時間）。
pub const HANDOFF_WAIT: Duration = Duration::from_secs(10);

/// `device.release` 請求（`source`＝`agent:p8`）。
pub fn release_req(reason: &str) -> SessionRequest {
    SessionRequest::Release {
        source: "agent:p8".into(),
        reason: PyValue::from(reason),
    }
}

/// 非同步送出 `device.release`（走 safety lane，同 RPC 路徑）。
pub fn release_async(rig: &ActorRig, reason: &str) -> Receiver<Result<PyDict, PyErr>> {
    rig.request_async(release_req(reason))
}

/// 送出 `device.release` 並等回覆字典。
pub fn release(rig: &ActorRig, reason: &str) -> PyDict {
    recv(&release_async(rig, reason), "device.release")
}

/// 送出 `device.attach`（`force` 只略過開檔前預檢）並等回覆字典。
pub fn device_attach(rig: &ActorRig, force: bool) -> PyDict {
    recv(&rig.request_async(SessionRequest::DeviceAttach { force }), "device.attach")
}

/// 收回覆（逾時或 `Err` 即 panic）。
pub fn recv(rx: &Receiver<Result<PyDict, PyErr>>, what: &str) -> PyDict {
    match rx.recv_timeout(HANDOFF_WAIT) {
        Ok(Ok(d)) => d,
        Ok(Err(e)) => panic!("{what} 應回字典，實際 Err({})", e.message),
        Err(e) => panic!("{what} 回覆逾時：{e}"),
    }
}

/// 回應的鍵序。
pub fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().cloned().collect()
}

/// 字串欄位。
pub fn str_field(d: &PyDict, k: &str) -> Option<String> {
    d.get(k).and_then(PyValue::as_str).map(str::to_string)
}

/// 回應內 `session.state`。
pub fn session_state(d: &PyDict) -> Option<String> {
    match d.get("session") {
        Some(PyValue::Dict(s)) => str_field(s, "state"),
        _ => None,
    }
}

/// 目前 `session.state`。
pub fn state(rig: &ActorRig) -> String {
    str_field(&rig.get_state(), "state").unwrap_or_default()
}

/// 等 FakeDevice 已收到 OS 提交（`base` 之後），代表前景命令的 TX 已交給假 OS。
pub fn wait_tx_in_flight(rig: &ActorRig, base: usize) {
    rig.wait_for("命令 TX 已交 OS", |r| r.dev.os_submits() > base);
}

/// 觀察窗：在 `window` 內 FakeDevice 寫出長度不得變化（零 TX 證據；不作同步點）。
pub fn assert_dut_silent(rig: &ActorRig, from: usize, window: Duration, what: &str) {
    let until = Instant::now() + window;
    while Instant::now() < until {
        assert_eq!(rig.dev.tx_len(), from, "{what}：DUT 端零新增 bytes（實際 {:?}）", rig.dev.tx_since(from));
        std::thread::sleep(Duration::from_millis(5));
    }
    let _ = rig.get_state();
    assert_eq!(rig.dev.tx_len(), from, "{what}：DUT 端零新增 bytes（實際 {:?}）", rig.dev.tx_since(from));
}

/// 讓 file push 停在 chunk 迴圈（chunk 0 完成、chunk 1 卡在第一個 echo slice）；回傳 push 的回覆接收端。
pub fn push_held_in_chunk_loop(x: &XferRig) -> Receiver<Result<PyDict, PyErr>> {
    let data: Vec<u8> = (0..1536u32).map(|i| (i * 7 % 251) as u8).collect();
    let local = x.tmp.write("handoff.bin", &data);
    x.peer
        .hold_at_line(2, sw_core::file_transfer::templates::DEFAULT_ECHO_SLICE_SIZE);
    let rx = x.transfer_async(push_req(&local, "/tmp/handoff.bin"));
    x.wait_peer_reached_hold();
    rx
}
