//! P8 Task 6：`file.push`／`file.pull` 的 session 整合測試台（`XferRig`／`ShellPeer`）。
//!
//! 以 P5 `ActorRig`（真 actor thread＋OpRunner＋唯一 TxWriter＋FakeDevice）組裝；DUT 端是 `ShellPeer`：
//! 背景 thread 讀取裝置**已確認寫出**的 bytes（外部 peer），逐 byte 回顯（`\n` 回 `\r\n`、`\x15` 清行），收到 `\n`
//! 時把整行交給 T4 的 `FakeShell` 解譯（虛擬檔案系統），輸出與 prompt `root@t:/# ` 回送為 RX。
//!
//! 外部證據一律取自 FakeDevice 的實際寫出（`dut_bytes()`／`dut_lines()`／`tx_len()`），「零 TX」「不重送」不以 WAL
//! 推論；WAL 只用於 META 與關聯斷言。`hold_after(n)` 讓 peer 在收到第 n 個 byte 後停住（不回顯、不執行），用來把
//! 傳輸停在已知位置，再由測試注入 quiet banner、送 release 等；`release_hold()` 後補處理暫存的 bytes。

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sw_core::file_transfer::{FileTransferKind, FileTransferRequest, TransferPort};
use sw_core::session_core::SessionRequest;
use sw_pal::fakes::serial::FakeDevice;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

use super::fake_shell::{FakeShell, ShellCaps};
use super::ActorRig;

/// ShellPeer 回送的 prompt（符合 `SHELL_PROFILE` 的 `(?m)^root@t:.*# `）。
pub const PEER_PROMPT: &str = "root@t:/# ";
/// 單次傳輸 RPC 的等待上限（真實時間；FakeClock 不推進時傳輸只靠 RX 前進）。
pub const XFER_WAIT: Duration = Duration::from_secs(30);

#[derive(Default)]
struct PeerState {
    /// 自 peer 啟動起收到的所有 bytes（DUT 端外部 oracle）。
    received: Vec<u8>,
    /// 已處理（回顯／執行）的 byte 數。
    processed: usize,
    /// 處理到第 n 個 byte 時停住；`None` 不停。
    hold_at: Option<usize>,
    /// 已完成 n 行、且目前行已處理 col 個 byte 時停住（一次性；觸發後轉成 `hold_at`）。
    hold_line: Option<(usize, usize)>,
    /// 目前未完成的輸入行。
    line: Vec<u8>,
    /// 已完成的輸入行（`\n` 結尾者；不含 `\x15` 清掉的行）。
    lines: Vec<String>,
    /// 在下一行輸出之前插入的 RX（模擬非同步輸出）。
    pending_inject: Vec<u8>,
}

/// DUT 端 shell 替身（外部 peer）。
pub struct ShellPeer {
    dev: FakeDevice,
    shell: Arc<Mutex<FakeShell>>,
    state: Arc<Mutex<PeerState>>,
    stop: Arc<AtomicBool>,
    /// 自 peer 啟動起裝置上已有的 TX 長度（之前的 login／ready probe 不計）。
    base: usize,
}

impl ShellPeer {
    pub fn start(dev: &FakeDevice, caps: ShellCaps) -> ShellPeer {
        let peer = ShellPeer {
            dev: dev.clone(),
            shell: Arc::new(Mutex::new(FakeShell::new(caps))),
            state: Arc::new(Mutex::new(PeerState::default())),
            stop: Arc::new(AtomicBool::new(false)),
            base: dev.tx_len(),
        };
        let dev = dev.clone();
        let shell = Arc::clone(&peer.shell);
        let state = Arc::clone(&peer.state);
        let stop = Arc::clone(&peer.stop);
        let base = peer.base;
        std::thread::Builder::new()
            .name("xfer-shell-peer".into())
            .spawn(move || {
                let mut cursor = base;
                while !stop.load(Ordering::Acquire) {
                    let new = dev.tx_since(cursor);
                    cursor += new.len();
                    let out = {
                        let mut st = state.lock().unwrap();
                        st.received.extend_from_slice(&new);
                        process(&mut st, &shell)
                    };
                    if !out.is_empty() {
                        dev.push_rx(&out);
                    }
                    if new.is_empty() {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                }
            })
            .expect("建立 ShellPeer thread");
        peer
    }

    /// 自 peer 啟動起 DUT 實際收到的 bytes。
    pub fn dut_bytes(&self) -> Vec<u8> {
        self.dev.tx_since(self.base)
    }

    /// DUT 實際收到的 byte 數（含 hold 中尚未處理者）。
    pub fn tx_len(&self) -> usize {
        self.dev.tx_len() - self.base
    }

    /// 已完成的輸入行。
    pub fn dut_lines(&self) -> Vec<String> {
        self.state.lock().unwrap().lines.clone()
    }

    /// 含 `needle` 的完整輸入行數。
    pub fn lines_containing(&self, needle: &str) -> usize {
        self.dut_lines().iter().filter(|l| l.contains(needle)).count()
    }

    /// 處理到 DUT 收到的第 `n` 個 byte（自 peer 啟動起算）時停住。
    pub fn hold_after(&self, n: usize) {
        self.state.lock().unwrap().hold_at = Some(n);
    }

    /// 已完成 `lines` 行、目前行已處理 `col` 個 byte 時停住（例如第 3 行命令的第一個 echo slice 之後）。
    pub fn hold_at_line(&self, lines: usize, col: usize) {
        self.state.lock().unwrap().hold_line = Some((lines, col));
    }

    /// 自目前已處理的位置停住（之後收到的 bytes 一律暫存）。
    pub fn hold_now(&self) {
        let mut st = self.state.lock().unwrap();
        st.hold_at = Some(st.processed);
    }

    /// 是否已停在 hold 點且 DUT 還有後續 bytes 送達（傳輸確實卡在 peer）。
    pub fn holding_with_input(&self) -> bool {
        let st = self.state.lock().unwrap();
        st.hold_at.is_some_and(|h| st.processed >= h && st.received.len() > st.processed)
    }

    /// 是否已處理到 hold 點（之後不一定有新 bytes）。`hold_at_line` 的條件在處理下一個 byte 時才檢查，
    /// 恰好停在行內第 col 個 byte 且尚無後續輸入時亦視為已到達。
    pub fn reached_hold(&self) -> bool {
        let st = self.state.lock().unwrap();
        st.hold_at.is_some_and(|h| st.processed >= h)
            || st.hold_line.is_some_and(|(n, col)| st.lines.len() >= n && st.line.len() >= col)
    }

    /// 解除 hold，補處理暫存的 bytes。
    pub fn release_hold(&self) {
        let out = {
            let mut st = self.state.lock().unwrap();
            st.hold_at = None;
            process(&mut st, &self.shell)
        };
        if !out.is_empty() {
            self.dev.push_rx(&out);
        }
    }

    /// 直接注入 DUT 輸出（例如 boot banner）。
    pub fn emit(&self, bytes: &[u8]) {
        self.dev.push_rx(bytes);
    }

    /// 在下一行命令輸出之前插入 `bytes`。
    pub fn inject_before_next_output(&self, bytes: &[u8]) {
        self.state.lock().unwrap().pending_inject.extend_from_slice(bytes);
    }

    /// DUT 虛擬檔案系統中的檔案內容。
    pub fn file(&self, path: &str) -> Option<Vec<u8>> {
        self.shell.lock().unwrap().file(path)
    }

    /// 在 DUT 虛擬檔案系統放一個檔案。
    pub fn put_file(&self, path: &str, data: &[u8]) {
        self.shell.lock().unwrap().put_file(path, data);
    }
}

impl Drop for ShellPeer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
    }
}

/// 處理 `received[processed..]`（到 hold 點為止），回傳要回送的 RX。
fn process(st: &mut PeerState, shell: &Arc<Mutex<FakeShell>>) -> Vec<u8> {
    let mut out = Vec::new();
    while st.processed < st.received.len() {
        if let Some((n, col)) = st.hold_line {
            if st.lines.len() >= n && st.line.len() >= col {
                st.hold_line = None;
                st.hold_at = Some(st.processed);
            }
        }
        if st.hold_at.is_some_and(|h| st.processed >= h) {
            break;
        }
        let b = st.received[st.processed];
        st.processed += 1;
        match b {
            0x15 => st.line.clear(),
            b'\n' => {
                out.extend_from_slice(b"\r\n");
                let line = String::from_utf8_lossy(&st.line).into_owned();
                st.line.clear();
                st.lines.push(line.clone());
                out.append(&mut st.pending_inject);
                out.extend_from_slice(run_line(shell, &line).as_bytes());
            }
            _ => {
                st.line.push(b);
                out.push(b);
            }
        }
    }
    out
}

/// 交 FakeShell 執行一行；去掉 FakeShell 自帶的回顯，換行改 `\r\n`，結尾 prompt 改 [`PEER_PROMPT`]。
fn run_line(shell: &Arc<Mutex<FakeShell>>, line: &str) -> String {
    if line.is_empty() {
        return PEER_PROMPT.to_string();
    }
    let mut sh = shell.lock().unwrap();
    let before = sh.rx_snapshot_len();
    let _ = sh.send_command(line, "peer", None);
    let text = sh.rx_text_from(before);
    let echo = format!("{line}\r\n");
    let body = text.strip_prefix(&echo).unwrap_or(&text);
    let body = body.strip_suffix("# ").unwrap_or(body);
    let mut s = body.replace("\r\n", "\n").replace('\n', "\r\n");
    s.push_str(PEER_PROMPT);
    s
}

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// 測試專用暫存目錄（daemon 端本地檔；`Drop` 時刪除）。
pub struct TempDir(PathBuf);

impl TempDir {
    pub fn new(tag: &str) -> TempDir {
        let n = TMP_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("sw-p8t6-{tag}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("建立暫存目錄");
        TempDir(dir)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }

    /// 寫一個本地檔並回傳其路徑字串。
    pub fn write(&self, name: &str, data: &[u8]) -> String {
        let p = self.0.join(name);
        std::fs::write(&p, data).expect("寫本地檔");
        p.to_string_lossy().into_owned()
    }

    pub fn join(&self, name: &str) -> String {
        self.0.join(name).to_string_lossy().into_owned()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// push 請求（RPC 層轉型後的預設值：chunk 512、ack auto、source agent）。
pub fn push_req(local: &str, remote: &str) -> FileTransferRequest {
    FileTransferRequest {
        kind: FileTransferKind::Push,
        selector: "COM0".into(),
        source: "agent".into(),
        local_path: Some(local.to_string()),
        remote_path: remote.to_string(),
        chunk_size: 512,
        chunk_timeout_s: None,
        ack_mode: "auto".into(),
    }
}

/// pull 請求。
pub fn pull_req(remote: &str, local: Option<&str>) -> FileTransferRequest {
    FileTransferRequest {
        kind: FileTransferKind::Pull,
        selector: "COM0".into(),
        source: "agent".into(),
        local_path: local.map(str::to_string),
        remote_path: remote.to_string(),
        chunk_size: 512,
        chunk_timeout_s: None,
        ack_mode: "auto".into(),
    }
}

/// ActorRig（READY）＋ShellPeer。
pub struct XferRig {
    pub rig: ActorRig,
    pub peer: ShellPeer,
    pub tmp: TempDir,
}

impl XferRig {
    /// `profile_yaml` 的 shell session 進 READY 後掛上 peer。
    pub fn ready(profile_yaml: &str, caps: ShellCaps) -> XferRig {
        let rig = ActorRig::new(profile_yaml).ready();
        let peer = ShellPeer::start(&rig.dev, caps);
        XferRig { rig, peer, tmp: TempDir::new("rig") }
    }

    /// 同 [`XferRig::ready`]，但 actor 附 ConsoleHub（PTY 替身 console；human raw ownership 測試用）。
    pub fn ready_with_hub(profile_yaml: &str, caps: ShellCaps) -> XferRig {
        let rig = ActorRig::new_with_hub(profile_yaml).ready();
        let peer = ShellPeer::start(&rig.dev, caps);
        XferRig { rig, peer, tmp: TempDir::new("hub") }
    }

    /// 送出 FileTransfer 請求並等回覆（`Err` 為 framing `EXCEPTION`）。
    pub fn transfer(&self, req: FileTransferRequest) -> Result<PyDict, PyErr> {
        self.transfer_async(req)
            .recv_timeout(XFER_WAIT)
            .expect("file transfer 回覆逾時")
    }

    /// 送出 FileTransfer 請求並預期回字典。
    pub fn transfer_ok(&self, req: FileTransferRequest) -> PyDict {
        match self.transfer(req) {
            Ok(d) => d,
            Err(e) => panic!("file transfer 應回字典，實際 Err({})", e.message),
        }
    }

    /// 非同步送出（之後 `recv_timeout` 取回）。
    pub fn transfer_async(
        &self,
        req: FileTransferRequest,
    ) -> crossbeam_channel::Receiver<Result<PyDict, PyErr>> {
        self.rig.request_async(SessionRequest::FileTransfer(req))
    }

    /// 等 `boot_quiet_remaining_s` 出現（banner 已被 actor 處理、fence 已 arm）。
    pub fn wait_quiet_armed(&self) {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let s = self.rig.get_state();
            if !matches!(s.get("boot_quiet_remaining_s"), None | Some(PyValue::Null)) {
                return;
            }
            assert!(Instant::now() < deadline, "banner 後 quiet 未 arm：{s:?}");
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// 等 peer 停在 hold 點且後續 bytes 已送達（傳輸確實卡住）。
    pub fn wait_peer_holding(&self) {
        wait_real("peer 停在 hold 點且有後續輸入", || self.peer.holding_with_input());
    }

    /// 等 peer 處理到 hold 點。
    pub fn wait_peer_reached_hold(&self) {
        wait_real("peer 處理到 hold 點", || self.peer.reached_hold());
    }

    /// 等 `foreground_busy` 變為 `busy`。
    pub fn wait_foreground_busy(&self, busy: bool) {
        wait_real(&format!("foreground_busy={busy}"), || {
            self.rig.get_state().get("foreground_busy") == Some(&PyValue::Bool(busy))
        });
    }

    /// WAL 內 TX 紀錄的 `cmd_id`（None 以空字串表示）。
    pub fn wal_tx_cmd_ids(&self) -> Vec<String> {
        self.rig
            .wal_rows()
            .into_iter()
            .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("TX"))
            .map(|r| r.get("cmd_id").and_then(PyValue::as_str).unwrap_or("").to_string())
            .collect()
    }
}

/// 實時輪詢直到條件成立（上限 10 s）。
pub fn wait_real(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// 回應的鍵序。
pub fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().cloned().collect()
}

/// 回應的 `error_code`。
pub fn code(d: &PyDict) -> Option<String> {
    d.get("error_code").and_then(PyValue::as_str).map(str::to_string)
}

/// 整數欄位。
pub fn int_of(d: &PyDict, k: &str) -> Option<i128> {
    match d.get(k) {
        Some(PyValue::Int(sw_proto::pyjson::PyInt::Small(v))) => Some(*v),
        _ => None,
    }
}
