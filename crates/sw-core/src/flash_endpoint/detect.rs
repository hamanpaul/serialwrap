//! P8 Task 9：sync-probe 認線偵測器（Python `flash_endpoint.py` `detect_mcu_line`／`ProbeTransport`／`DetectResult`）。
//!
//! 演算法逐字對照 Python：排除 `command_capable` 候選；其餘候選依序嘗試 registry 全部 pattern，首個命中即換下一個
//! 候選（同一候選不重複計）；命中 0 → `None`、1 → `Matched`、>1 → `Ambiguous`（不自動挑選）。`by_id` 為 `None` 的
//! 候選直接略過，不呼叫 [`ProbeTransport::probe`]（等價 Python `probe(None, …)` 因 `by_id_to_com.get(None)` 恆 False）。

use crate::mcu_patterns::McuPatternRegistry;
use crate::session_manager::FlashCandidate;

/// 偵測結果狀態（Python `"matched" | "ambiguous" | "none"`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DetectStatus {
    Matched,
    Ambiguous,
    None,
}

impl DetectStatus {
    /// Python `DetectResult.status` 字串（`mcu.status` 的 `last_detect.status`）。
    pub fn as_str(&self) -> &'static str {
        match self {
            DetectStatus::Matched => "matched",
            DetectStatus::Ambiguous => "ambiguous",
            DetectStatus::None => "none",
        }
    }
}

/// sync-probe 偵測結果（Python `DetectResult`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetectResult {
    pub status: DetectStatus,
    /// 只有 `Matched` 時有值。
    pub by_id: Option<String>,
    /// 只有 `Matched` 時有值。
    pub family: Option<String>,
    /// 只有 `Ambiguous` 時列出全部命中的 by_id。
    pub hits: Vec<String>,
}

/// 對指定 by_id 送出 probe 並回報是否在 `timeout_ms` 內收到含 `expect` 的回應（Python `ProbeTransport`）。
pub trait ProbeTransport {
    fn probe(&mut self, by_id: &str, probe_bytes: &[u8], expect: &[u8], timeout_ms: u64) -> bool;
}

/// Python `detect_mcu_line(candidates, registry, transport)`。
pub fn detect_mcu_line(cands: &[FlashCandidate], reg: &McuPatternRegistry, t: &mut dyn ProbeTransport) -> DetectResult {
    // 步驟 1：排除 command_capable console（例如已被 broker 佔用的 terminal）。
    let mut hits: Vec<(String, String)> = Vec::new();
    for c in cands.iter().filter(|c| !c.command_capable) {
        let Some(by_id) = c.by_id.as_deref() else {
            continue;
        };
        // 步驟 2：逐 pattern 嘗試，首個命中即停止（同一候選不重複計入）。
        for p in reg.all() {
            if t.probe(by_id, &p.probe, &p.expect, p.timeout_ms) {
                hits.push((by_id.to_string(), p.family.clone()));
                break;
            }
        }
    }
    // 步驟 3：依命中數回傳結果。
    match hits.len() {
        0 => DetectResult { status: DetectStatus::None, by_id: None, family: None, hits: Vec::new() },
        1 => {
            let (by_id, family) = hits.remove(0);
            DetectResult { status: DetectStatus::Matched, by_id: Some(by_id), family: Some(family), hits: Vec::new() }
        }
        _ => DetectResult {
            status: DetectStatus::Ambiguous,
            by_id: None,
            family: None,
            hits: hits.into_iter().map(|(b, _)| b).collect(),
        },
    }
}
