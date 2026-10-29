//! P7 Task 19（08-slice23-login-events.md）：`SW_TEST_COM_PAIR` peer 端（COMb）的腳本化 DUT。
//!
//! - 以 sw-pal serial backend（`sw_pal::native().serial`）開 peer 端，背景執行緒持續讀取並記錄收到的全部 bytes。
//! - 反應 DSL 與 `scripts/rust/p7_fake_bridge.py` 的 `ScriptedBridge` reactions 相同：每收到一行（去 `\r`、以 `\n` 結尾）
//!   依序找第一個符合的 reaction（`Eq`＝整行相等、`Prefix`＝前綴、`Secret`＝等於 secret、`Any`＝任何行），
//!   `after` 後寫出其 `rx`。`rx` 內 `{rest}` 代換為該行去掉前綴後的部分（供 `echo __READY__<nonce>` 回 nonce 行）。
//! - 平台限制（`scripts/rust/check_cfg_boundary.py`）：不得用平台 cfg；COM 名稱與 handle 皆經 sw-pal。
//!   只在 `cap:windows_com` 案例使用（缺 `SW_TEST_COM_PAIR` 時案例保持 ignored，arch §9.4）。
#![allow(dead_code)]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use sw_pal::traits::{
    CancelToken, CloseToken, DevicePath, IoWaker, ReadOutcome, SerialConfig, SerialReader,
    SerialWriter,
};

/// reaction 的比對方式（同 `ScriptedBridge`）。
#[derive(Clone, Debug)]
pub enum On {
    Eq(String),
    Prefix(String),
    Secret(String),
    Any,
}

#[derive(Clone, Debug)]
pub struct Reaction {
    pub on: On,
    pub rx: String,
    pub after: Duration,
}

impl Reaction {
    pub fn new(on: On, rx: &str) -> Reaction {
        Reaction {
            on,
            rx: rx.to_string(),
            after: Duration::ZERO,
        }
    }

    fn reply(&self, line: &str) -> Option<String> {
        let rest = match &self.on {
            On::Eq(want) | On::Secret(want) if line == want => "",
            On::Prefix(p) if line.starts_with(p.as_str()) => &line[p.len()..],
            On::Any => line,
            _ => return None,
        };
        Some(self.rx.replace("{rest}", rest))
    }
}

/// `SW_TEST_COM_PAIR=COMa:COMb`（擁有者 host 為 `COM20:COM21`）；未設定回 `None`。
pub fn com_pair() -> Option<(String, String)> {
    let raw = std::env::var("SW_TEST_COM_PAIR").ok()?;
    let (a, b) = raw.split_once(':')?;
    Some((a.trim().to_ascii_uppercase(), b.trim().to_ascii_uppercase()))
}

struct Shared {
    received: Vec<u8>,
    line: Vec<u8>,
    reactions: Vec<Reaction>,
}

/// peer 端 DUT：收到的 bytes 全數記錄；`emit` 直接寫出（模擬 DUT 主動輸出，如 U-Boot banner）。
pub struct ComPeer {
    shared: Arc<Mutex<Shared>>,
    writer: Arc<Mutex<Box<dyn SerialWriter>>>,
    cancel: Arc<CancelToken>,
    stop: Arc<AtomicBool>,
    waker: Arc<dyn IoWaker>,
    close: Option<CloseToken>,
    reader: Option<JoinHandle<Box<dyn SerialReader>>>,
}

impl ComPeer {
    /// 以 115200 8N1、無流控開 peer 端（與 profile 預設 `uart.*` 相同）。
    pub fn open(port: &str, reactions: Vec<Reaction>) -> ComPeer {
        let cfg = SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false);
        let handle = sw_pal::native()
            .serial
            .open_exclusive(&DevicePath(port.to_string()), &cfg)
            .unwrap_or_else(|e| panic!("開啟 peer 端 {port} 失敗：{e:?}"));
        let (mut reader, writer, waker, close) = handle.split();
        let shared = Arc::new(Mutex::new(Shared {
            received: Vec::new(),
            line: Vec::new(),
            reactions,
        }));
        let writer = Arc::new(Mutex::new(writer));
        let cancel = Arc::new(CancelToken::new(waker.clone()));
        let stop = Arc::new(AtomicBool::new(false));
        let (s2, w2, stop2, c2) = (shared.clone(), writer.clone(), stop.clone(), cancel.clone());
        let reader_thread = std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            while !stop2.load(Ordering::SeqCst) {
                let n = match reader.read(&mut buf, Some(Duration::from_millis(200))) {
                    Ok(ReadOutcome::Data(n)) => n,
                    Ok(_) => continue,
                    Err(_) => break,
                };
                let mut replies = Vec::new();
                {
                    let mut g = s2.lock().unwrap();
                    g.received.extend_from_slice(&buf[..n]);
                    for &b in &buf[..n] {
                        if b != b'\n' {
                            if b != b'\r' {
                                g.line.push(b);
                            }
                            continue;
                        }
                        let line = String::from_utf8_lossy(&g.line).into_owned();
                        g.line.clear();
                        if let Some((rx, after)) = g
                            .reactions
                            .iter()
                            .find_map(|r| r.reply(&line).map(|rx| (rx, r.after)))
                        {
                            replies.push((rx, after));
                        }
                    }
                }
                for (rx, after) in replies {
                    std::thread::sleep(after);
                    write_all(&w2, &c2, rx.as_bytes());
                }
            }
            reader
        });
        ComPeer {
            shared,
            writer,
            cancel,
            stop,
            waker,
            close: Some(close),
            reader: Some(reader_thread),
        }
    }

    /// DUT 主動輸出（不經 reaction）。
    pub fn emit(&self, bytes: &[u8]) {
        write_all(&self.writer, &self.cancel, bytes);
    }

    /// 目前為止收到的全部 bytes。
    pub fn received(&self) -> Vec<u8> {
        self.shared.lock().unwrap().received.clone()
    }

    /// 收到 bytes 的總數（作為之後「零 byte」斷言的起點）。
    pub fn mark(&self) -> usize {
        self.shared.lock().unwrap().received.len()
    }

    pub fn received_since(&self, mark: usize) -> Vec<u8> {
        self.shared.lock().unwrap().received[mark..].to_vec()
    }

    /// 等到收到的 bytes 含 `needle`；逾時回 false。
    pub fn wait_contains(&self, needle: &[u8], limit: Duration) -> bool {
        let end = Instant::now() + limit;
        loop {
            if self
                .received()
                .windows(needle.len().max(1))
                .any(|w| w == needle)
            {
                return true;
            }
            if Instant::now() >= end {
                return false;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

fn write_all(writer: &Arc<Mutex<Box<dyn SerialWriter>>>, cancel: &CancelToken, mut bytes: &[u8]) {
    let mut w = writer.lock().unwrap();
    while !bytes.is_empty() {
        match w.write(bytes, cancel).outcome {
            sw_pal::traits::TxOutcome::Completed(n) if n > 0 => {
                bytes = &bytes[n.min(bytes.len())..]
            }
            other => panic!("peer 端寫出失敗：{other:?}"),
        }
    }
}

impl Drop for ComPeer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.waker.wake();
        let reader = self.reader.take().and_then(|h| h.join().ok());
        if let (Some(reader), Some(close)) = (reader, self.close.take()) {
            if let Ok(writer) = Arc::try_unwrap(std::mem::replace(
                &mut self.writer,
                Arc::new(Mutex::new(Box::new(NullWriter) as Box<dyn SerialWriter>)),
            )) {
                close.close(reader, writer.into_inner().unwrap());
            }
        }
    }
}

/// Drop 時替換寫端用的空實作（原寫端交還 `CloseToken::close`）。
struct NullWriter;

impl SerialWriter for NullWriter {
    fn write_gated(
        &mut self,
        _data: &[u8],
        _cancel: &CancelToken,
        _gate: &dyn sw_pal::traits::SubmitGate,
    ) -> sw_pal::traits::TxResult {
        sw_pal::traits::TxResult {
            outcome: sw_pal::traits::TxOutcome::NotSubmitted,
            error: None,
        }
    }
}
