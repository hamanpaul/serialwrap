//! CLI 上下文與輸出寫入器（P3-T11 紅燈 stub）。
#![allow(unused_variables)]

/// 文字輸出包裝：`\n` 依平台轉為 `newline`（Q-05）。
// 欄位於 P3-T11 實作前尚未讀取；僅此型別豁免 dead_code，不以 crate 層級遮蔽。
#[allow(dead_code)]
pub struct TextIo<'a> {
    out: &'a mut dyn std::io::Write,
    err: &'a mut dyn std::io::Write,
    newline: &'static str,
}

impl<'a> TextIo<'a> {
    pub fn new(out: &'a mut dyn std::io::Write, err: &'a mut dyn std::io::Write, newline: &'static str) -> Self {
        todo!("P3-T11: TextIo::new")
    }
    /// `"\n"` → newline（Q-05）。
    pub fn out_str(&mut self, s: &str) -> std::io::Result<()> {
        todo!("P3-T11: TextIo::out_str")
    }
    /// 寫入後立即 flush（Q-06）。
    pub fn err_str(&mut self, s: &str) -> std::io::Result<()> {
        todo!("P3-T11: TextIo::err_str")
    }
}

pub struct CliCtx<'a> {
    pub p: &'a sw_pal::ClientPlatform,
    pub env: sw_proto::runtime_paths::EnvSnapshot,
    pub paths: sw_proto::runtime_paths::RuntimePaths,
    pub io: TextIo<'a>,
    pub verbose: i64,
}

/// 非 TTY：`BufWriter` 8192；TTY：`LineWriter`（Q-06）。
pub fn stdout_writer(is_terminal: bool) -> Box<dyn std::io::Write> {
    todo!("P3-T11: stdout_writer")
}
