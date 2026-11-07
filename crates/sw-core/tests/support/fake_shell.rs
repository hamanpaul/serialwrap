//! P8-T4 測試替身：`FakeShell` 實作 `TransferPort`，以虛擬檔案系統解譯 T3 樣板產生的命令。
//!
//! 只認 T3 樣板，其他命令輸出 `sh: <cmd>: not found`。每次送出先把命令文字當 echo 加到 RX，
//! 再依 `ShellCaps` 加入輸出與 prompt `# `。

use std::collections::{BTreeMap, VecDeque};
use std::sync::Mutex;
use std::time::Duration;

use sw_core::file_transfer::templates as t;
use sw_core::file_transfer::{
    EchoPaced, LocalFiles, RejectReason, TransferAbort, TransferIds, TransferPort, TxRejection,
};
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyre::PyRegex;

#[derive(Clone)]
pub struct ShellCaps {
    pub base64: bool,
    pub openssl: bool,
    pub md5sum: bool,
    pub supports_echo_paced: bool,
    /// 命令超過此字元數時截斷並不執行。
    pub line_limit: Option<usize>,
    pub echo_stall_after_chars: Option<usize>,
    pub corrupt_md5: bool,
    pub mv_fails: bool,
    /// 某 cmd_id 後綴不回 prompt。
    pub drop_prompt_on: Option<&'static str>,
    pub inject_noise: Option<String>,
    /// 第 n 次（1 起算，含被拒的嘗試）送出回 `TxRejected`。
    pub reject_on_send_n: Option<(usize, TxRejection)>,
    pub probe_shell_error: bool,
    pub rx_max_chars: Option<usize>,
    /// 命令輸出（echo 之外）不立即進 RX，每次 `wait_for_regex_from` 釋出這麼多字元（模擬慢速 UART）。
    pub release_per_wait: Option<usize>,
    /// 只對 cmd_id 以此結尾的命令套用 `release_per_wait`。
    pub release_only_suffix: Option<&'static str>,
    /// 第 n 個（0 起算）chunk 命令的 decoder 失敗。
    pub decoder_fails_chunk: Option<usize>,
    pub drop_end_marker: bool,
    /// 取代 pull 輸出的 base64 本體。
    pub pull_payload: Option<String>,
}

impl ShellCaps {
    pub fn busybox_base64() -> Self {
        ShellCaps {
            base64: true,
            openssl: false,
            md5sum: true,
            supports_echo_paced: true,
            line_limit: None,
            echo_stall_after_chars: None,
            corrupt_md5: false,
            mv_fails: false,
            drop_prompt_on: None,
            inject_noise: None,
            reject_on_send_n: None,
            probe_shell_error: false,
            rx_max_chars: None,
            release_per_wait: None,
            release_only_suffix: None,
            decoder_fails_chunk: None,
            drop_end_marker: false,
            pull_payload: None,
        }
    }
    pub fn openssl_only() -> Self {
        ShellCaps { base64: false, openssl: true, ..Self::busybox_base64() }
    }
    pub fn no_tools() -> Self {
        ShellCaps { base64: false, openssl: false, ..Self::busybox_base64() }
    }
}

pub fn rej(reason: RejectReason, rejected_confirmed: usize) -> TxRejection {
    TxRejection { reason, rejected_confirmed, unknown: false }
}

pub struct FakeShell {
    caps: ShellCaps,
    files: BTreeMap<String, Vec<u8>>,
    rx: String,
    trimmed: u64,
    pending: String,
    attempts: usize,
    commands: Vec<String>,
    cmd_ids: Vec<String>,
    paced_ids: Vec<String>,
    raw_tx: Vec<Vec<u8>>,
    chunk_seen: usize,
    pub wait_calls: usize,
}

impl FakeShell {
    pub fn new(caps: ShellCaps) -> Self {
        FakeShell {
            caps,
            files: BTreeMap::new(),
            rx: String::new(),
            trimmed: 0,
            pending: String::new(),
            attempts: 0,
            commands: vec![],
            cmd_ids: vec![],
            paced_ids: vec![],
            raw_tx: vec![],
            chunk_seen: 0,
            wait_calls: 0,
        }
    }
    pub fn put_file(&mut self, path: &str, data: &[u8]) {
        self.files.insert(path.to_string(), data.to_vec());
    }
    pub fn file(&self, path: &str) -> Option<Vec<u8>> {
        self.files.get(path).cloned()
    }
    pub fn cmd_ids(&self) -> Vec<String> {
        self.cmd_ids.clone()
    }
    pub fn paced_cmd_ids(&self) -> Vec<String> {
        self.paced_ids.clone()
    }
    pub fn commands(&self) -> Vec<String> {
        self.commands.clone()
    }
    /// 含被拒絕的嘗試。
    pub fn send_attempts(&self) -> usize {
        self.attempts
    }
    pub fn raw_tx_count(&self, bytes: &[u8]) -> usize {
        self.raw_tx.iter().filter(|b| b.as_slice() == bytes).count()
    }

    fn append_rx(&mut self, s: &str) {
        self.rx.push_str(s);
        if let Some(max) = self.caps.rx_max_chars {
            let len = self.rx.chars().count();
            if len > max {
                let cut = len - max;
                let byte = self.rx.char_indices().nth(cut).map_or(self.rx.len(), |(i, _)| i);
                self.rx.drain(..byte);
                self.trimmed += cut as u64;
            }
        }
    }

    fn release(&mut self) {
        let Some(n) = self.caps.release_per_wait else { return };
        let take = self.pending.chars().count().min(n);
        let byte = self.pending.char_indices().nth(take).map_or(self.pending.len(), |(i, _)| i);
        let part: String = self.pending.drain(..byte).collect();
        self.append_rx(&part);
    }

    fn check_reject(&mut self) -> Result<(), TransferAbort> {
        self.attempts += 1;
        if let Some((n, r)) = self.caps.reject_on_send_n {
            if n == self.attempts {
                return Err(TransferAbort::rejected(r));
            }
        }
        Ok(())
    }

    fn execute(&mut self, cmd: &str, cmd_id: Option<&str>) {
        self.commands.push(cmd.to_string());
        if let Some(id) = cmd_id {
            self.cmd_ids.push(id.to_string());
        }
        if let Some(limit) = self.caps.line_limit {
            if cmd.chars().count() > limit {
                let cut: String = cmd.chars().take(limit).collect();
                self.append_rx(&format!("{cut}\r\n# "));
                return;
            }
        }
        self.append_rx(&format!("{cmd}\r\n"));
        let mut out = String::new();
        if let Some(noise) = &self.caps.inject_noise {
            out.push_str(noise);
        }
        out.push_str(&self.run(cmd));
        let dropped = match (self.caps.drop_prompt_on, cmd_id) {
            (Some(suffix), Some(id)) => id.ends_with(suffix),
            _ => false,
        };
        if !dropped {
            out.push_str("# ");
        }
        let slow = self.caps.release_per_wait.is_some()
            && match (self.caps.release_only_suffix, cmd_id) {
                (None, _) => true,
                (Some(suffix), Some(id)) => id.ends_with(suffix),
                (Some(_), None) => false,
            };
        if slow {
            self.pending.push_str(&out);
        } else {
            self.append_rx(&out);
        }
    }

    fn run(&mut self, cmd: &str) -> String {
        if cmd == t::decoder_probe_command() {
            return self.probe(cmd, "SW_XFER_DECODER_B64_42", "SW_XFER_DECODER_OPENSSL_42");
        }
        if cmd == t::encoder_probe_command() {
            return self.probe(cmd, "SW_XFER_ENCODER_B64_42", "SW_XFER_ENCODER_OPENSSL_42");
        }
        if let Some(rest) = cmd.strip_prefix("printf '%s' ") {
            return self.run_chunk(rest);
        }
        if let Some(rest) = cmd.strip_prefix("md5sum ") {
            let (path, _) = unquote_first(rest);
            if !self.caps.md5sum {
                return "sh: md5sum: not found\n".into();
            }
            return match self.files.get(&path) {
                None => format!("md5sum: {path}: No such file or directory\n"),
                Some(d) => {
                    let mut h = t::md5_hex(d);
                    if self.caps.corrupt_md5 {
                        let first = if h.starts_with('0') { "1" } else { "0" };
                        h.replace_range(..1, first);
                    }
                    format!("{h}  {path}\n{}\n", t::MD5_SENTINEL)
                }
            };
        }
        if let Some(rest) = cmd.strip_prefix("mv ") {
            let (src, rest) = unquote_first(rest);
            let (dst, _) = unquote_first(rest.trim_start());
            if self.caps.mv_fails {
                return "mv: cannot move\n".into();
            }
            return match self.files.remove(&src) {
                Some(d) => {
                    self.files.insert(dst, d);
                    format!("{}\n", t::MV_SENTINEL)
                }
                None => format!("mv: cannot stat '{src}'\n"),
            };
        }
        if let Some(rest) = cmd.strip_prefix("rm -f ") {
            let (path, _) = unquote_first(rest);
            self.files.remove(&path);
            return format!("{}\n", t::CLEANUP_SENTINEL);
        }
        if cmd.starts_with("printf '%s\\n' '===SW_XFER_'BEGIN'===' && ") {
            return self.run_pull(cmd);
        }
        format!("sh: {cmd}: not found\n")
    }

    fn probe(&self, _cmd: &str, b64: &str, ossl: &str) -> String {
        if self.caps.probe_shell_error {
            "sh: 1: syntax error: unexpected \"then\"\n".into()
        } else if self.caps.base64 {
            format!("{b64}\n")
        } else if self.caps.openssl {
            format!("{ossl}\n")
        } else {
            String::new()
        }
    }

    fn run_chunk(&mut self, rest: &str) -> String {
        let (payload, rest) = unquote_first(rest);
        let Some(rest) = rest.trim_start().strip_prefix("| ") else { return "sh: syntax error\n".into() };
        let (tool, rest) = if let Some(r) = rest.strip_prefix("base64 -d ") {
            ("base64", r)
        } else if let Some(r) = rest.strip_prefix("openssl enc -base64 -d -A ") {
            ("openssl", r)
        } else {
            return "sh: syntax error\n".into();
        };
        let have = if tool == "base64" { self.caps.base64 } else { self.caps.openssl };
        let (op, rest) = unquote_first(rest);
        let (path, _) = unquote_first(rest.trim_start());
        let idx = self.chunk_seen;
        self.chunk_seen += 1;
        if !have {
            return format!("sh: {tool}: not found\n");
        }
        if self.caps.decoder_fails_chunk == Some(idx) {
            return format!("{tool}: invalid input\n");
        }
        let Ok(data) = a2b_base64_lenient(payload.as_bytes()) else {
            return format!("{tool}: invalid input\n");
        };
        if op == ">" {
            self.files.insert(path, data);
        } else {
            self.files.entry(path).or_default().extend_from_slice(&data);
        }
        format!("{}\n", t::CHUNK_SENTINEL)
    }

    fn run_pull(&mut self, cmd: &str) -> String {
        let begin = format!("{}\n", t::SENTINEL_BEGIN);
        let fail = format!("{}\n", t::ENCODER_FAILURE_SENTINEL);
        let after = cmd.split_once(" && ").unwrap().1;
        let (tool, wrap, rest) = if let Some(r) = after.strip_prefix("base64 < ") {
            ("base64", 76usize, r)
        } else if let Some(r) = after.strip_prefix("openssl enc -base64 < ") {
            ("openssl", 64usize, r)
        } else {
            return format!("{begin}sh: syntax error\n{fail}");
        };
        let (path, _) = unquote_first(rest);
        let have = if tool == "base64" { self.caps.base64 } else { self.caps.openssl };
        if !have {
            return format!("{begin}sh: {tool}: not found\n{fail}");
        }
        let Some(data) = self.files.get(&path) else {
            return format!("{begin}sh: can't open '{path}': No such file or directory\n{fail}");
        };
        let b64 = self.caps.pull_payload.clone().unwrap_or_else(|| t::b64encode(data));
        let mut body = String::new();
        let chars: Vec<char> = b64.chars().collect();
        for line in chars.chunks(wrap) {
            body.extend(line);
            body.push('\n');
        }
        if self.caps.drop_end_marker {
            return format!("{begin}{body}");
        }
        format!("{begin}{body}{}\n", t::SENTINEL_END)
    }
}

/// 解析一個 shell 詞（支援 `'…'`、`"…"`、裸字元拼接），回傳 (值, 剩餘)。
fn unquote_first(s: &str) -> (String, &str) {
    let mut out = String::new();
    let mut i = 0;
    while i < s.len() {
        let c = s[i..].chars().next().unwrap();
        match c {
            '\'' => {
                let j = s[i + 1..].find('\'').expect("未結束的單引號") + i + 1;
                out.push_str(&s[i + 1..j]);
                i = j + 1;
            }
            '"' => {
                let j = s[i + 1..].find('"').expect("未結束的雙引號") + i + 1;
                out.push_str(&s[i + 1..j]);
                i = j + 1;
            }
            ' ' => break,
            _ => {
                out.push(c);
                i += c.len_utf8();
            }
        }
    }
    (out, &s[i..])
}

impl TransferPort for FakeShell {
    fn send_command(&mut self, cmd: &str, _source: &str, cmd_id: Option<&str>) -> Result<(), TransferAbort> {
        self.check_reject()?;
        self.raw_tx.push(format!("{cmd}\n").into_bytes());
        self.execute(cmd, cmd_id);
        Ok(())
    }

    fn supports_echo_paced(&self) -> bool {
        self.caps.supports_echo_paced
    }

    fn send_command_echo_paced(
        &mut self,
        cmd: &str,
        _source: &str,
        cmd_id: Option<&str>,
        slice_size: usize,
        _echo_timeout: Duration,
    ) -> Result<EchoPaced, TransferAbort> {
        self.check_reject()?;
        if let Some(id) = cmd_id {
            self.paced_ids.push(id.to_string());
        }
        let slice = slice_size.max(1);
        let total = cmd.chars().count();
        if let Some(limit) = self.caps.echo_stall_after_chars {
            let (mut acked, mut sent) = (0usize, 0usize);
            while sent < total {
                let len = slice.min(total - sent);
                sent += len;
                if acked + len <= limit {
                    acked += len;
                } else {
                    let echoed: String = cmd.chars().take(acked).collect();
                    self.raw_tx.push(cmd.chars().take(sent).collect::<String>().into_bytes());
                    self.append_rx(&echoed);
                    return Ok(EchoPaced { ok: false, acked_chars: acked, sent_chars: sent });
                }
            }
        }
        self.raw_tx.push(format!("{cmd}\n").into_bytes());
        self.execute(cmd, cmd_id);
        Ok(EchoPaced { ok: true, acked_chars: total, sent_chars: total })
    }

    fn cancel_input_line(&mut self, _source: &str) -> Result<(), TransferAbort> {
        self.check_reject()?;
        self.raw_tx.push(b"\x15\n".to_vec());
        self.append_rx("\r\n# ");
        Ok(())
    }

    fn rx_snapshot_len(&self) -> u64 {
        self.trimmed + self.rx.chars().count() as u64
    }

    fn rx_text_from(&self, from_offset: u64) -> String {
        if from_offset <= self.trimmed {
            return self.rx.clone();
        }
        let rel = (from_offset - self.trimmed) as usize;
        let byte = self.rx.char_indices().nth(rel).map_or(self.rx.len(), |(i, _)| i);
        self.rx[byte..].to_string()
    }

    fn wait_for_regex_from(&mut self, re: &PyRegex, from_offset: u64, timeout: Duration) -> Result<bool, TransferAbort> {
        self.wait_calls += 1;
        if timeout.is_zero() {
            return Ok(false);
        }
        self.release();
        Ok(re.search(&self.rx_text_from(from_offset)).is_some())
    }
}

/// 記憶體虛擬本地檔案系統。
pub struct MemFiles {
    inner: Mutex<BTreeMap<String, Vec<u8>>>,
    fail_write: Mutex<Vec<String>>,
}

impl MemFiles {
    pub fn empty() -> Self {
        MemFiles { inner: Mutex::new(BTreeMap::new()), fail_write: Mutex::new(vec![]) }
    }
    pub fn with(path: &str, data: &[u8]) -> Self {
        let f = Self::empty();
        f.inner.lock().unwrap().insert(path.to_string(), data.to_vec());
        f
    }
    pub fn get(&self, path: &str) -> Option<Vec<u8>> {
        self.inner.lock().unwrap().get(path).cloned()
    }
    pub fn fail_write_on(&self, path: &str) {
        self.fail_write.lock().unwrap().push(path.to_string());
    }
}

impl LocalFiles for MemFiles {
    fn is_file(&self, path: &str) -> bool {
        self.inner.lock().unwrap().contains_key(path)
    }
    fn read_all(&self, path: &str) -> Result<Vec<u8>, String> {
        self.get(path).ok_or_else(|| format!("[Errno 2] No such file or directory: '{path}'"))
    }
    fn write_truncate(&self, path: &str, data: &[u8]) -> Result<(), String> {
        if self.fail_write.lock().unwrap().iter().any(|p| p == path) {
            return Err(format!("[Errno 13] Permission denied: '{path}'"));
        }
        self.inner.lock().unwrap().insert(path.to_string(), data.to_vec());
        Ok(())
    }
}

pub struct FixedIds {
    queue: VecDeque<String>,
}

impl FixedIds {
    pub fn new(ids: &[&str]) -> Self {
        FixedIds { queue: ids.iter().map(|s| s.to_string()).collect() }
    }
}

impl TransferIds for FixedIds {
    fn hex(&mut self, n: usize) -> String {
        let v = self.queue.pop_front().expect("FixedIds 用盡");
        assert_eq!(v.len(), n, "hex({n}) 取得長度不符的 id {v}");
        v
    }
}
