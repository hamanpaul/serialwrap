//! Slice 2.4 檔案傳輸（P8）：T3 提供板端命令樣板與 RX 判定 helper，T4 提供 `push_file`／`pull_file` 引擎。
//!
//! 引擎只依賴 [`TransferPort`]（T5 的 [`OpTransferPort`] 實作它），不直接碰 `OpCtx`／`TxWriter`；
//! 也不自行判定 RPC 錯誤碼——只回傳 Python 相同鍵序的 `PyDict` 或 [`TransferAbort`]（OD-62）。

pub mod op_port;
pub mod rx_match;
pub mod templates;

pub use op_port::OpTransferPort;

use std::time::Duration;

use rx_match as m;
use sw_proto::pycompat::{a2b_base64_lenient, PyOsError};
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::pypath::{basename, PathFlavor};
use sw_proto::pyre::{compile, PyFlags, PyRegex};
use templates as t;

/// echo-ACK 節流結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EchoPaced {
    pub ok: bool,
    pub acked_chars: usize,
    pub sent_chars: usize,
}

/// 被拒那一筆 TX 的分類（OD-62）。`Other` 含 Flash／Wal／OwnerMismatch／部分寫入失敗／Unknown。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RejectReason {
    Quiet,
    Cancelled,
    StaleGeneration,
    Draining,
    Other,
}

/// TX 被拒的證據：session 層（T6）依此映射 `AUTOBOOT_QUIET`／`COMMAND_ABORTED_IN_FLIGHT`／
/// `FLUSHED_BY_RECOVERY`／`TRANSFER_TIMEOUT`。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TxRejection {
    pub reason: RejectReason,
    /// 被拒那一筆已確認寫出的 bytes（0 表示零寫出）。
    pub rejected_confirmed: usize,
    /// 結果不明（Unknown）。
    pub unknown: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransferAbort {
    /// 傳輸中 TX 被拒。`TransferPort` 回傳時 `chunks`／`transfer_cmd_id` 留空，由引擎補齊：
    /// chunk 迴圈內 `chunks=Some((idx,total))`，其餘階段 `None`。
    TxRejected { rej: TxRejection, chunks: Option<(usize, usize)>, transfer_cmd_id: String },
    /// daemon 端本地檔讀寫失敗（訊息為 Python `str(OSError)` 格式）。
    LocalIo(String),
    /// profile `prompt_regex` 無法編譯。
    BadPromptRegex(String),
}

impl TransferAbort {
    /// `TransferPort` 實作用的建構式（`chunks`／`transfer_cmd_id` 由引擎補齊）。
    pub fn rejected(rej: TxRejection) -> Self {
        TransferAbort::TxRejected { rej, chunks: None, transfer_cmd_id: String::new() }
    }
}

/// 引擎看到的 UART 介面。所有方法回 `Err(TxRejected)` 時，引擎立即上拋且不再送任何命令（含 cleanup）。
pub trait TransferPort {
    fn send_command(&mut self, cmd: &str, source: &str, cmd_id: Option<&str>) -> Result<(), TransferAbort>;
    fn supports_echo_paced(&self) -> bool;
    fn send_command_echo_paced(
        &mut self,
        cmd: &str,
        source: &str,
        cmd_id: Option<&str>,
        slice_size: usize,
        echo_timeout: Duration,
    ) -> Result<EchoPaced, TransferAbort>;
    fn cancel_input_line(&mut self, source: &str) -> Result<(), TransferAbort>;
    /// RX 絕對偏移（字元計）。
    fn rx_snapshot_len(&self) -> u64;
    /// 自絕對偏移 `from_offset` 起的 RX 文字；偏移已被視窗修剪時回整個視窗。
    fn rx_text_from(&self, from_offset: u64) -> String;
    /// 自 `from_offset` 起等待 `re` 命中；`timeout` 為零時不做任何檢查直接回 `false`（EVT-D-F1）。
    fn wait_for_regex_from(&mut self, re: &PyRegex, from_offset: u64, timeout: Duration) -> Result<bool, TransferAbort>;
}

pub trait LocalFiles: Send + Sync {
    fn is_file(&self, path: &str) -> bool;
    /// `Err` 為 `str(OSError)`。
    fn read_all(&self, path: &str) -> Result<Vec<u8>, String>;
    /// `open(p,"wb").write(data)`：非原子、不建立目錄。
    fn write_truncate(&self, path: &str, data: &[u8]) -> Result<(), String>;
}

/// `std::fs` 實作；相對路徑以 daemon cwd 為準。
pub struct StdLocalFiles;

fn os_error_text(e: &std::io::Error, path: &str) -> String {
    let raw = e.to_string();
    let strerror = match raw.rfind(" (os error ") {
        Some(i) => raw[..i].to_string(),
        None => raw,
    };
    PyOsError {
        errno: e.raw_os_error().map(i64::from),
        winerror: None,
        strerror: Some(strerror),
        filename: Some(path.to_string()),
        plain: None,
    }
    .to_string()
}

impl LocalFiles for StdLocalFiles {
    fn is_file(&self, path: &str) -> bool {
        std::path::Path::new(path).is_file()
    }
    fn read_all(&self, path: &str) -> Result<Vec<u8>, String> {
        std::fs::read(path).map_err(|e| os_error_text(&e, path))
    }
    fn write_truncate(&self, path: &str, data: &[u8]) -> Result<(), String> {
        std::fs::write(path, data).map_err(|e| os_error_text(&e, path))
    }
}

/// `uuid4().hex[:n]`。
pub trait TransferIds {
    fn hex(&mut self, n: usize) -> String;
}

#[derive(Debug, Clone)]
pub struct PushParams {
    pub local_path: String,
    pub remote_path: String,
    pub chunk_size: i128,
    pub timeout: Duration,
    pub prompt_regex: String,
    pub source: String,
    pub ack_mode: String,
    pub echo_slice_size: usize,
    pub echo_timeout: Duration,
    pub max_console_line_chars: Option<i128>,
}

#[derive(Debug, Clone)]
pub struct PullParams {
    pub remote_path: String,
    pub local_path: Option<String>,
    pub timeout: Duration,
    pub prompt_regex: String,
    pub source: String,
    pub max_console_line_chars: Option<i128>,
    pub path_flavor: PathFlavor,
}

/// 分段 pull 單次輪詢的等待上限（OD-80）。引擎每次最多等這麼久就把已到達的 RX 搬進本地緩衝，
/// 因此單次 pull 的大小不受 RX 視窗（131072 字元）限制，只要輪詢速度快過視窗被填滿的速度。
pub const PULL_POLL_SLICE: Duration = Duration::from_millis(250);

/// 無換行的超長片段超過此字元數時整段搬走，避免行首永遠停在視窗前緣而被修剪。
const PULL_FORCE_DRAIN_CHARS: usize = 65_536;

type Res<T> = Result<T, TransferAbort>;

fn s(v: &str) -> PyValue {
    PyValue::Str(v.to_string())
}
fn n(v: i128) -> PyValue {
    PyValue::Int(sw_proto::pyjson::PyInt::Small(v))
}

fn err(code: &str) -> PyDict {
    let mut d = PyDict::new();
    d.insert("ok", PyValue::Bool(false));
    d.insert("error_code", s(code));
    d
}

fn err_progress(code: &str, idx: usize, total: usize) -> PyDict {
    let mut d = err(code);
    d.insert("chunks_sent", n(idx as i128));
    d.insert("chunks_total", n(total as i128));
    d
}

fn line_limit_error(budget: Option<i128>) -> PyDict {
    let mut d = err("CONSOLE_LINE_LIMIT_TOO_SMALL");
    d.insert("max_console_line_chars", budget.map(n).unwrap_or(PyValue::Null));
    d
}

/// `_validate_line_budget`：`<=0` → LIMIT error。
fn validate_budget(budget: Option<i128>) -> Option<PyDict> {
    match budget {
        Some(b) if b <= 0 => Some(line_limit_error(budget)),
        _ => None,
    }
}

/// 把 `TransferPort` 回的 `TxRejected` 補上進度與傳輸 cmd_id 前綴。
struct Tag<'a> {
    prefix: &'a str,
}
impl Tag<'_> {
    fn with(&self, e: TransferAbort, chunks: Option<(usize, usize)>) -> TransferAbort {
        match e {
            TransferAbort::TxRejected { rej, .. } => {
                TransferAbort::TxRejected { rej, chunks, transfer_cmd_id: self.prefix.to_string() }
            }
            other => other,
        }
    }
}

fn compile_prompt(pattern: &str) -> Res<PyRegex> {
    compile(pattern, PyFlags(0)).map_err(|e| TransferAbort::BadPromptRegex(format!("{e:?}")))
}

/// 一次「送出、等 prompt、回 RX 文字」。逾時回 `Ok(None)`。
struct Session<'a> {
    port: &'a mut dyn TransferPort,
    tag: Tag<'a>,
    source: &'a str,
    timeout: Duration,
    prompt_regex: &'a str,
    re: Option<PyRegex>,
}

impl Session<'_> {
    fn re(&mut self) -> Res<&PyRegex> {
        if self.re.is_none() {
            self.re = Some(compile_prompt(self.prompt_regex)?);
        }
        Ok(self.re.as_ref().unwrap())
    }

    fn send(&mut self, cmd: &str, cmd_id: &str, chunks: Option<(usize, usize)>) -> Res<()> {
        self.port.send_command(cmd, self.source, Some(cmd_id)).map_err(|e| self.tag.with(e, chunks))
    }

    fn wait(&mut self, from: u64, chunks: Option<(usize, usize)>) -> Res<bool> {
        let timeout = self.timeout;
        self.re()?;
        let re = self.re.take().unwrap();
        let r = self.port.wait_for_regex_from(&re, from, timeout);
        self.re = Some(re);
        r.map_err(|e| self.tag.with(e, chunks))
    }

    /// 送出 `cmd` 並等 prompt；逾時 `Ok(None)`，否則回 `pre` 之後的 RX 文字。
    fn run(&mut self, cmd: &str, cmd_id: &str, chunks: Option<(usize, usize)>) -> Res<Option<String>> {
        let pre = self.port.rx_snapshot_len();
        self.send(cmd, cmd_id, chunks)?;
        if !self.wait(pre, chunks)? {
            return Ok(None);
        }
        Ok(Some(self.port.rx_text_from(pre)))
    }

    /// `_cleanup_remote`：盡力清暫存檔，結果忽略；但被拒仍要上拋。
    fn cleanup(&mut self, path: &str, prefix: &str, chunks: Option<(usize, usize)>) -> Res<()> {
        self.run(&t::cleanup_command(path), &format!("{prefix}-cleanup"), chunks).map(|_| ())
    }

    /// `_remote_md5`。
    fn remote_md5(&mut self, path: &str, prefix: &str) -> Res<Option<String>> {
        let cmd = t::md5_command(path);
        Ok(self
            .run(&cmd, &format!("{prefix}-md5"), None)?
            .and_then(|raw| m::remote_md5_from_output(&raw, &cmd)))
    }

    /// `_probe_decoder`／`_probe_encoder`：回 `(工具, 錯誤碼)`。
    fn probe(&mut self, decoder: bool, prefix: &str) -> Res<Result<&'static str, &'static str>> {
        let (cmd, sentinels, missing) = if decoder {
            (
                t::decoder_probe_command(),
                [t::DECODER_B64_SENTINEL, t::DECODER_OPENSSL_SENTINEL],
                "TARGET_DECODER_MISSING",
            )
        } else {
            (
                t::encoder_probe_command(),
                [t::ENCODER_B64_SENTINEL, t::ENCODER_OPENSSL_SENTINEL],
                "TARGET_ENCODER_MISSING",
            )
        };
        let Some(raw) = self.run(&cmd, &format!("{prefix}-probe"), None)? else {
            return Ok(Err("TRANSFER_TIMEOUT"));
        };
        if m::runtime_sentinel_seen(&raw, sentinels[0], Some(&cmd)) {
            return Ok(Ok("base64"));
        }
        if m::runtime_sentinel_seen(&raw, sentinels[1], Some(&cmd)) {
            return Ok(Ok("openssl"));
        }
        if m::probe_shell_error(&raw, &cmd) {
            return Ok(Err("TRANSFER_PROBE_FAILED"));
        }
        Ok(Err(missing))
    }
}

/// `push_file`：host → target。
pub fn push_file(
    port: &mut dyn TransferPort,
    files: &dyn LocalFiles,
    ids: &mut dyn TransferIds,
    p: &PushParams,
) -> Res<PyDict> {
    if !t::ACK_MODES.contains(&p.ack_mode.as_str()) {
        let mut d = err("INVALID_ARGS");
        d.insert("ack_mode", s(&p.ack_mode));
        return Ok(d);
    }
    if let Some(e) = validate_budget(p.max_console_line_chars) {
        return Ok(e);
    }
    if p.chunk_size <= 0 {
        let mut d = err("INVALID_ARGS");
        d.insert("chunk_size", n(p.chunk_size));
        return Ok(d);
    }
    if !files.is_file(&p.local_path) {
        let mut d = err("LOCAL_FILE_NOT_FOUND");
        d.insert("local_path", s(&p.local_path));
        return Ok(d);
    }
    let supports = port.supports_echo_paced();
    let paced = match p.ack_mode.as_str() {
        "none" => false,
        "echo" if !supports => return Ok(err("ECHO_ACK_UNSUPPORTED")),
        _ => supports,
    };

    let data = files.read_all(&p.local_path).map_err(TransferAbort::LocalIo)?;
    let md5_expected = t::md5_hex(&data);
    let tmp = format!("/tmp/.sw_upload_{}", ids.hex(12));
    let prefix = format!("ft-{}", ids.hex(8));
    let budget = p.max_console_line_chars;

    let fixed = [t::md5_command(&tmp), t::mv_command(&tmp, &p.remote_path), t::cleanup_command(&tmp)];
    if t::line_budget_exceeded(&fixed.iter().map(String::as_str).collect::<Vec<_>>(), budget) {
        return Ok(line_limit_error(budget));
    }
    if t::line_budget_exceeded(&[t::decoder_probe_command().as_str()], budget) {
        return Ok(line_limit_error(budget));
    }

    let raw_capacity: i128;
    if let Some(b) = budget {
        let empties: Vec<String> = t::DECODERS
            .iter()
            .flat_map(|d| [">", ">>"].into_iter().map(move |op| (*d, op)))
            .map(|(d, op)| t::chunk_command("", d, op, &tmp).expect("固定 decoder"))
            .collect();
        let max_overhead = empties.iter().map(|c| c.len() as i128).max().unwrap_or(0);
        raw_capacity = ((b - max_overhead).div_euclid(4) * 3).max(0);
        // Python 的 `//` 為 floor division；`(b - overhead)` 為負時結果被 max(0) 吸收。
        if t::line_budget_exceeded(&empties.iter().map(String::as_str).collect::<Vec<_>>(), budget) {
            return Ok(line_limit_error(budget));
        }
        if !data.is_empty() && raw_capacity < 1 {
            return Ok(line_limit_error(budget));
        }
    } else {
        raw_capacity = p.chunk_size;
    }

    let mut sess = Session {
        port,
        tag: Tag { prefix: &prefix },
        source: &p.source,
        timeout: p.timeout,
        prompt_regex: &p.prompt_regex,
        re: None,
    };

    let decoder = match sess.probe(true, &prefix)? {
        Ok(d) => d,
        Err(code) => return Ok(err(code)),
    };

    let effective = p.chunk_size.min(raw_capacity);
    if !data.is_empty() && effective < 1 {
        return Ok(line_limit_error(budget));
    }
    if budget.is_some() {
        let probe_size = (effective.max(0) as usize).min(data.len());
        let probe_chunk = t::b64encode(&vec![0u8; probe_size]);
        let cmds: Vec<String> = [">", ">>"]
            .iter()
            .map(|op| t::chunk_command(&probe_chunk, decoder, op, &tmp).expect("固定 decoder"))
            .collect();
        if t::line_budget_exceeded(&cmds.iter().map(String::as_str).collect::<Vec<_>>(), budget) {
            return Ok(line_limit_error(budget));
        }
    }

    let chunks = t::split_chunks(&data, effective.max(1) as usize);
    let total = chunks.len();

    for (idx, chunk) in chunks.iter().enumerate() {
        let prog = Some((idx, total));
        let op = if idx == 0 { ">" } else { ">>" };
        let cmd = t::chunk_command(&t::b64encode(chunk), decoder, op, &tmp).expect("decoder 已探測");
        if t::line_budget_exceeded(&[cmd.as_str()], budget) {
            sess.cleanup(&tmp, &prefix, prog)?;
            return Ok(line_limit_error(budget));
        }
        let pre = sess.port.rx_snapshot_len();
        let cmd_id = format!("{prefix}-{idx}");
        if paced {
            let r = sess
                .port
                .send_command_echo_paced(&cmd, &p.source, Some(&cmd_id), p.echo_slice_size, p.echo_timeout)
                .map_err(|e| sess.tag.with(e, prog))?;
            if !r.ok {
                sess.port.cancel_input_line(&p.source).map_err(|e| sess.tag.with(e, prog))?;
                sess.cleanup(&tmp, &prefix, prog)?;
                let mut d = err_progress("TRANSFER_ECHO_STALL", idx, total);
                d.insert("acked_chars", n(r.acked_chars as i128));
                d.insert("sent_chars", n(r.sent_chars as i128));
                return Ok(d);
            }
        } else {
            sess.send(&cmd, &cmd_id, prog)?;
        }
        if !sess.wait(pre, prog)? {
            sess.cleanup(&tmp, &prefix, prog)?;
            return Ok(err_progress("TRANSFER_TIMEOUT", idx, total));
        }
        if !m::runtime_sentinel_seen(&sess.port.rx_text_from(pre), t::CHUNK_SENTINEL, Some(&cmd)) {
            sess.cleanup(&tmp, &prefix, prog)?;
            return Ok(err_progress("TARGET_DECODER_FAILED", idx, total));
        }
    }

    match sess.remote_md5(&tmp, &prefix)? {
        None => {
            sess.cleanup(&tmp, &prefix, None)?;
            return Ok(err("CHECKSUM_VERIFY_FAILED"));
        }
        Some(actual) if actual != md5_expected => {
            sess.cleanup(&tmp, &prefix, None)?;
            let mut d = err("CHECKSUM_MISMATCH");
            d.insert("expected", s(&md5_expected));
            d.insert("actual", s(&actual));
            return Ok(d);
        }
        Some(_) => {}
    }

    let mv = t::mv_command(&tmp, &p.remote_path);
    if t::line_budget_exceeded(&[mv.as_str()], budget) {
        sess.cleanup(&tmp, &prefix, None)?;
        return Ok(line_limit_error(budget));
    }
    let Some(raw) = sess.run(&mv, &format!("{prefix}-mv"), None)? else {
        return Ok(err("MOVE_TIMEOUT"));
    };
    if !m::runtime_sentinel_seen(&raw, t::MV_SENTINEL, Some(&mv)) {
        return Ok(err("MOVE_FAILED"));
    }

    let mut d = PyDict::new();
    d.insert("ok", PyValue::Bool(true));
    d.insert("bytes", n(data.len() as i128));
    d.insert("chunks", n(total as i128));
    d.insert("md5", s(&md5_expected));
    d.insert("remote_path", s(&p.remote_path));
    Ok(d)
}

/// 分段收集 pull 的輸出（OD-80）：不依賴 RX 視窗，逐輪把已到達的完整行搬進本地緩衝。
///
/// 回 `Ok(None)` 表示在 `timeout` 內沒等到 prompt。
/// 取捨：若引擎輪詢慢於視窗被填滿的速度，被修剪的資料會漏掉；這會讓後續 md5 比對失敗
/// （`CHECKSUM_MISMATCH`），不會靜默成功。
fn collect_until_prompt(sess: &mut Session<'_>, pre: u64) -> Res<Option<String>> {
    let mut acc = String::new();
    let mut offset = pre;
    let mut remaining = sess.timeout;
    sess.re()?;
    loop {
        let slice = remaining.min(PULL_POLL_SLICE);
        let re = sess.re.take().unwrap();
        let hit = sess.port.wait_for_regex_from(&re, offset, slice);
        sess.re = Some(re);
        let hit = hit.map_err(|e| sess.tag.with(e, None))?;
        let text = sess.port.rx_text_from(offset);
        if hit {
            acc.push_str(&text);
            return Ok(Some(acc));
        }
        // 只搬到最後一個換行：未完成的行與可能被切開的 prompt 留在視窗內，下一輪重讀。
        let keep = match text.rfind('\n') {
            Some(i) => i + 1,
            None if text.chars().count() > PULL_FORCE_DRAIN_CHARS => text.len(),
            None => 0,
        };
        acc.push_str(&text[..keep]);
        offset += text[..keep].chars().count() as u64;
        remaining = remaining.saturating_sub(slice);
        if remaining.is_zero() {
            return Ok(None);
        }
    }
}

/// `pull_file`：target → host。
pub fn pull_file(
    port: &mut dyn TransferPort,
    files: &dyn LocalFiles,
    ids: &mut dyn TransferIds,
    p: &PullParams,
) -> Res<PyDict> {
    let local_path = match &p.local_path {
        Some(l) => l.clone(),
        None => basename(p.path_flavor, &p.remote_path),
    };
    if let Some(e) = validate_budget(p.max_console_line_chars) {
        return Ok(e);
    }
    let prefix = format!("ft-{}", ids.hex(8));
    let budget = p.max_console_line_chars;

    let md5_cmd = t::md5_command(&p.remote_path);
    let probe_cmd = t::encoder_probe_command();
    if t::line_budget_exceeded(&[probe_cmd.as_str(), md5_cmd.as_str()], budget) {
        return Ok(line_limit_error(budget));
    }
    let candidates: Vec<String> = t::ENCODERS
        .iter()
        .map(|e| t::pull_command(&p.remote_path, e).expect("固定 encoder"))
        .collect();
    if t::line_budget_exceeded(&candidates.iter().map(String::as_str).collect::<Vec<_>>(), budget) {
        return Ok(line_limit_error(budget));
    }

    let mut sess = Session {
        port,
        tag: Tag { prefix: &prefix },
        source: &p.source,
        timeout: p.timeout,
        prompt_regex: &p.prompt_regex,
        re: None,
    };

    let encoder = match sess.probe(false, &prefix)? {
        Ok(e) => e,
        Err(code) => return Ok(err(code)),
    };

    let cmd = t::pull_command(&p.remote_path, encoder).expect("encoder 已探測");
    if t::line_budget_exceeded(&[cmd.as_str()], budget) {
        return Ok(line_limit_error(budget));
    }
    let pre = sess.port.rx_snapshot_len();
    sess.send(&cmd, &format!("{prefix}-b64"), None)?;
    let Some(raw) = collect_until_prompt(&mut sess, pre)? else {
        return Ok(err("TRANSFER_TIMEOUT"));
    };

    let Some(b64) = m::extract_between_sentinels(&raw, Some(&cmd)) else {
        if m::failure_sentinel_seen(&raw, t::ENCODER_FAILURE_SENTINEL, Some(&cmd)) {
            return Ok(err("TARGET_ENCODER_FAILED"));
        }
        return Ok(err("PULL_PARSE_FAILED"));
    };

    // `base64.b64decode(str)`：含非 ASCII → ValueError；其餘非字母表字元丟棄，padding 錯誤 → binascii.Error。
    if !b64.is_ascii() {
        return Ok(err("BASE64_DECODE_FAILED"));
    }
    let Ok(data) = a2b_base64_lenient(b64.as_bytes()) else {
        return Ok(err("BASE64_DECODE_FAILED"));
    };

    let md5_local = t::md5_hex(&data);
    let Some(md5_remote) = sess.remote_md5(&p.remote_path, &prefix)? else {
        return Ok(err("CHECKSUM_VERIFY_FAILED"));
    };
    if md5_remote != md5_local {
        let mut d = err("CHECKSUM_MISMATCH");
        d.insert("expected", s(&md5_remote));
        d.insert("actual", s(&md5_local));
        return Ok(d);
    }

    files.write_truncate(&local_path, &data).map_err(TransferAbort::LocalIo)?;

    let mut d = PyDict::new();
    d.insert("ok", PyValue::Bool(true));
    d.insert("bytes", n(data.len() as i128));
    d.insert("md5", s(&md5_local));
    d.insert("local_path", s(&local_path));
    Ok(d)
}
