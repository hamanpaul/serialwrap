//! CLI 上下文與輸出寫入器（對應 `sw_core/cli.py` 的 `sys.stdout`／`sys.stderr` 使用方式）。
use std::io::{self, BufWriter, LineWriter, Write};

/// 文字輸出包裝：`\n` 依平台轉為 `newline`（Q-05）。
///
/// stdout 與 stderr 共用同一換行規則：Python 在 Windows 上 `sys.stdout`／`sys.stderr` 皆為文字模式，
/// 會把 `\n` 寫成 `\r\n`，因此 stderr 也必須轉換，否則與基線的位元組不一致。
pub struct TextIo<'a> {
    out: &'a mut dyn Write,
    err: &'a mut dyn Write,
    newline: &'static str,
}

impl<'a> TextIo<'a> {
    /// 建立文字輸出包裝；`newline` 由呼叫端依 `ClientCaps::text_newline` 傳入。
    pub fn new(out: &'a mut dyn Write, err: &'a mut dyn Write, newline: &'static str) -> Self {
        TextIo { out, err, newline }
    }

    /// `"\n"` → newline（Q-05）。不主動 flush，緩衝策略由 [`stdout_writer`] 決定。
    pub fn out_str(&mut self, s: &str) -> io::Result<()> {
        write_translated(&mut *self.out, s, self.newline)
    }

    /// 寫入後立即 flush（Q-06）：stderr 錯誤行必須在 stdout 的 JSON 之前可見。
    pub fn err_str(&mut self, s: &str) -> io::Result<()> {
        write_translated(&mut *self.err, s, self.newline)?;
        self.err.flush()
    }
}

/// 把 `s` 內每個 `\n` 寫成 `newline`，整段以單次 `write_all` 寫出；`newline` 為 `"\n"` 時原樣寫出。
///
/// 單次寫入讓緩衝層的 flush 邊界與 Python 一致：轉換後長度 ≥ 緩衝容量（8192）時於寫入當下直送，讀端已關閉即回錯
/// （CRLF 下本體 8190 位元組加換行共 8192 時，Python 於寫入當下以 exit 1 結束）；較短者留在緩衝、於結束時 flush（exit 120）。
/// 分段寫入會讓 8192 邊界的 stdout 停留在緩衝、與 stderr 的先後順序與錯誤時機都與 Python 不符（Q-06、Q-20）。
fn write_translated(w: &mut dyn Write, s: &str, newline: &str) -> io::Result<()> {
    if newline == "\n" {
        w.write_all(s.as_bytes())
    } else {
        w.write_all(s.replace('\n', newline).as_bytes())
    }
}

pub struct CliCtx<'a> {
    pub p: &'a sw_pal::ClientPlatform,
    pub env: sw_proto::runtime_paths::EnvSnapshot,
    pub paths: sw_proto::runtime_paths::RuntimePaths,
    pub io: TextIo<'a>,
    pub verbose: i64,
    /// 與 daemon 共用編譯輸入的 CLI 版本（管理面與 RPC 版本警告皆使用）。
    pub cli_version: &'static str,
}

/// 非 TTY：`BufWriter` 8192（與 Python 區塊緩衝一致，Q-06）；TTY：`LineWriter`。
///
/// 8192 與 `io.DEFAULT_BUFFER_SIZE` 相同：單次寫入超過容量時直接寫出，讀端已關閉時於寫入當下失敗（Q-20 的 exit 1）。
pub fn stdout_writer(is_terminal: bool) -> Box<dyn Write> {
    if is_terminal {
        Box::new(LineWriter::new(io::stdout()))
    } else {
        Box::new(BufWriter::with_capacity(8192, io::stdout()))
    }
}
