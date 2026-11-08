//! `sw-proto::pyre` 對 CPython 3.12.3 `re` 與 `valid_completion_regex` 的 golden 比對（P3-T5）。
//! 紅燈階段：`pyre::compile` 等為 `todo!`，測試以 `not yet implemented: P3-T5` 失敗。
mod common;

use std::collections::HashSet;
use sw_proto::pyre::{self, CompileError, PyFlags};

#[test]
fn golden_compile_search_completion() {
    let mut fails = Vec::new();
    for r in common::golden("pyre.tsv") {
        let (pat, hay) = r.input.split_once('\u{1f}').unwrap_or((r.input.as_str(), ""));
        let got = match r.op.as_str() {
            "compile" => match pyre::compile(pat, PyFlags::default()) {
                Ok(_) => "ok".to_string(),
                Err(CompileError::Re(e)) => format!("err:{e}"),
                Err(CompileError::Recursion) => "recursion".into(),
                Err(CompileError::Overflow(_)) => "overflow".into(),
                Err(CompileError::Untranslatable(_)) => "untranslatable".into(),
            },
            "search" => match pyre::compile(pat, PyFlags::default()) {
                Ok(rx) => rx.search(hay).map(|m| format!("{},{}", m.start, m.end)).unwrap_or("none".into()),
                Err(_) => "compile-failed".into(),
            },
            "completion_valid" => {
                let v = sw_proto::pyjson::PyValue::Str(pat.to_string());
                match pyre::completion_regex_check(Some(&v)) {
                    pyre::CompletionCheck::Valid => "1".into(),
                    pyre::CompletionCheck::Invalid => "0".into(),
                    pyre::CompletionCheck::Crash(_) => "crash".into(),
                }
            }
            o => panic!("{o}"),
        };
        if got != r.expected && !common::residual_accepts(pat) {   // 只在 Rust compile(pat) 回 Untranslatable(登記構造名) 時為真
            fails.push(format!("{} {} {:?}: got {got:?} want {:?}", r.id, r.op, pat, r.expected));
        }
    }
    assert!(fails.is_empty(), "{} 筆不符：\n{}", fails.len(), fails[..fails.len().min(30)].join("\n"));
}

#[test]
fn completion_none_is_valid_and_empty_invalid() {
    assert!(pyre::valid_completion_regex(None));
    assert!(!pyre::valid_completion_regex(Some(&"".into())));
    assert!(!pyre::valid_completion_regex(Some(&sw_proto::pyjson::PyValue::Int(sw_proto::pyjson::PyInt::Small(1)))));
}

#[test]
fn search_reports_code_point_offsets() {
    let rx = pyre::compile("é+x", PyFlags::default()).unwrap();
    assert_eq!(rx.search("aééx").map(|m| (m.start, m.end)), Some((1, 4)));
}

/// 殘餘檔衛生檢查：無重複列、每列都被 golden pattern 用到（否則過期）、核可欄非空、總筆數 ≤ 20、
/// 不含出貨 profile regex 與 `UBOOT_FALLBACK_PROMPTS`。紅燈階段殘餘檔為空，本測試直接通過。
#[test]
fn residuals_file_hygiene() {
    let rows = common::residual_rows();
    assert!(rows.len() <= 20, "殘餘檔 {} 筆，超過上限 20", rows.len());

    let mut seen = HashSet::new();
    for r in &rows {
        assert!(seen.insert(r.pattern_hex.as_str()), "殘餘檔重複列：{}", r.pattern_hex);
        assert!(!r.construct.trim().is_empty(), "構造名為空：{}", r.pattern_hex);
        assert!(!r.approver.trim().is_empty(), "核可者為空：{}", r.pattern_hex);
        assert!(!r.record.trim().is_empty(), "核可紀錄為空：{}", r.pattern_hex);
    }

    let used: HashSet<String> = common::golden("pyre.tsv")
        .iter()
        .filter(|r| r.op == "compile")
        .map(|r| r.input.bytes().map(|b| format!("{b:02x}")).collect::<String>())
        .collect();
    for r in &rows {
        assert!(used.contains(&r.pattern_hex), "殘餘列未被任何 golden pattern 使用（已過期）：{}", r.pattern_hex);
    }

    let shipped = common::shipped_regexes();
    for r in &rows {
        assert!(
            !shipped.iter().any(|s| s == &r.pattern),
            "殘餘列命中出貨 profile／UBOOT_FALLBACK_PROMPTS regex：{:?}",
            r.pattern
        );
    }
}
