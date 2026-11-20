//! P6 Task 2：Telnet 協定層（逐案對照 `tests/test_telnet_filter.py`，34 個 Python item 全數承接；07 Task 2 Step 1）。
#![allow(dead_code)]

mod common;
use common::golden::{rows, unhex, XorShift};
use sw_core::telnet_console::{escape_iac, TelnetFeed, TelnetFilter, TELNET_GREETING};
const IAC: u8 = 0xFF; const WILL: u8 = 0xFB; const WONT: u8 = 0xFC; const DO: u8 = 0xFD;
const DONT: u8 = 0xFE; const SB: u8 = 0xFA; const SE: u8 = 0xF0;
const BIN: u8 = 0x00; const ECHO: u8 = 0x01; const SGA: u8 = 0x03; const TTYPE: u8 = 0x18; const NAWS: u8 = 0x1F;

const GOLDEN: &str = include_str!("../../../contracts/golden/telnet_filter.tsv");

fn f(uart: &[u8], reply: &[u8]) -> TelnetFeed { TelnetFeed { uart: uart.to_vec(), reply: reply.to_vec() } }

#[test] fn greeting_bytes() {
    assert_eq!(TELNET_GREETING, [IAC,WILL,ECHO, IAC,WILL,SGA, IAC,DO,SGA, IAC,WILL,BIN]);
}
#[test] fn cr_emitted_immediately_at_chunk_end() {
    let mut t = TelnetFilter::new();
    assert_eq!(t.feed(b"\r"), f(b"\r", b""));
    assert_eq!(t.feed(b"x"), f(b"x", b""));
}
#[test] fn dont_echo_gets_wont_once_only() {
    let mut t = TelnetFilter::new();
    assert_eq!(t.feed(&[IAC, DONT, ECHO]), f(b"", &[IAC, WONT, ECHO]));
    assert_eq!(t.feed(&[IAC, DONT, ECHO]), f(b"", b""));
    // 被 DONT 關閉後無法重開：DO ECHO 回 WONT（06 §8.4 注意事項）
    assert_eq!(t.feed(&[IAC, DO, ECHO]), f(b"", &[IAC, WONT, ECHO]));
}
#[test] fn no_ff_returns_same_object() {
    let p = b"hello world";
    assert!(matches!(escape_iac(p), std::borrow::Cow::Borrowed(x) if std::ptr::eq(x, &p[..])));
}
#[test] fn golden_matches_python_byte_for_byte() {
    let mut n = 0;
    for r in rows(GOLDEN) {   // op == "telnet_feed"
        let mut t = TelnetFilter::new();
        let chunks: Vec<&str> = if r.input.is_empty() { vec![] } else { r.input.split(',').collect() };
        let outs: Vec<&str> = if r.expected.is_empty() { vec![] } else { r.expected.split(',').collect() };
        assert_eq!(chunks.len(), outs.len(), "case {}", r.id);
        for (c, o) in chunks.iter().zip(&outs) {
            let (uart, reply) = o.split_once('/').unwrap();
            let got = t.feed(&unhex(c));
            assert_eq!((got.uart, got.reply), (unhex(uart), unhex(reply)), "case {}", r.id);
        }
        n += 1;
    }
    assert!(n >= 2000, "golden 案例數 {n}");
}
const PICK: [u8; 17] = [IAC, WILL, WONT, DO, DONT, SB, SE, 0x00, 0x0A, 0x0D, ECHO, SGA, TTYPE, NAWS, 0xF1, 0xF6, b'a'];   // 涵蓋所有狀態轉移
#[test] fn chunking_is_invisible() {
    // 同一串 bytes，任意切割後的 uart／reply 串接必須等於一次餵入的結果（SW_TEST_SEED 決定序列，預設 0x5EED）
    let seed: u64 = std::env::var("SW_TEST_SEED").ok().and_then(|s| s.parse().ok()).unwrap_or(0x5EED);
    let mut rng = XorShift::new(seed);
    for _ in 0..5000 {
        let data: Vec<u8> = (0..rng.below(64)).map(|_| PICK[rng.below(PICK.len() as u64) as usize]).collect();
        let whole = TelnetFilter::new().feed(&data);
        let mut t = TelnetFilter::new(); let mut acc = TelnetFeed::default(); let mut i = 0;
        while i < data.len() { let n = 1 + rng.below((data.len() - i) as u64) as usize;
            let part = t.feed(&data[i..i + n]); acc.uart.extend(part.uart); acc.reply.extend(part.reply); i += n; }
        assert_eq!(acc, whole, "data={data:02x?}");
    }
}

/// 工具自測（07 Task 2 Step 1）：把第一列 expected 改掉一個 hex 字元後，以同一套比對邏輯（逐 chunk 的
/// `split_once('/')`＋`unhex`）重新比對，必須出現不相等；證明 golden 竄改會被 `golden_matches_python_byte_for_byte` 抓到。
#[test] fn golden_tamper_is_detected() {
    let all = rows(GOLDEN);
    let first = all.iter().find(|r| r.expected.chars().any(|c| c.is_ascii_hexdigit())).expect("golden 至少一列有 hex");
    let pos = first.expected.find(|c: char| c.is_ascii_hexdigit()).unwrap();
    let orig_ch = first.expected.as_bytes()[pos] as char;
    let repl = if orig_ch == '0' { '1' } else { '0' };
    let mut tampered = first.expected.clone();
    tampered.replace_range(pos..pos + 1, &repl.to_string());
    let parse = |s: &str| -> Vec<(Vec<u8>, Vec<u8>)> {
        s.split(',').map(|o| { let (u, r) = o.split_once('/').unwrap(); (unhex(u), unhex(r)) }).collect()
    };
    assert_ne!(parse(&tampered), parse(&first.expected), "竄改一個 hex 字元必須改變比對結果（case {}）", first.id);
}

fn feeds(inputs: &[&[u8]]) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut t = TelnetFilter::new();
    inputs.iter().map(|i| { let o = t.feed(i); (o.uart, o.reply) }).collect()
}
fn one(input: &[u8]) -> (Vec<u8>, Vec<u8>) { feeds(&[input]).remove(0) }
fn p(u: &[u8], r: &[u8]) -> (Vec<u8>, Vec<u8>) { (u.to_vec(), r.to_vec()) }
fn cat(parts: &[&[u8]]) -> Vec<u8> { parts.concat() }

#[test] fn plain_ascii() { assert_eq!(one(b"ls -la"), p(b"ls -la", b"")); }
#[test] fn high_bytes_passthrough() { assert_eq!(one(&[0x80, 0xFE, 0x01]), p(&[0x80, 0xFE, 0x01], b"")); }
#[test] fn iac_iac_unescapes_to_single_ff() { assert_eq!(one(&cat(&[b"a", &[IAC, IAC], b"b"])), p(b"a\xffb", b"")); }
#[test] fn iac_iac_split_across_feeds() { assert_eq!(feeds(&[&[IAC], &[IAC]]), vec![p(b"", b""), p(&[0xFF], b"")]); }
#[test] fn cr_nul_folds_to_cr() { assert_eq!(one(b"PING\r\x00"), p(b"PING\r", b"")); }
#[test] fn cr_lf_folds_to_cr() { assert_eq!(one(b"PING\r\n"), p(b"PING\r", b"")); }
#[test] fn cr_then_lf_across_boundary_swallows_lf() { assert_eq!(feeds(&[b"\r", b"\n"]), vec![p(b"\r", b""), p(b"", b"")]); }
#[test] fn cr_then_nul_across_boundary_swallows_nul() { assert_eq!(feeds(&[b"\r", &[0x00]]), vec![p(b"\r", b""), p(b"", b"")]); }
#[test] fn consecutive_crs_all_emitted() { assert_eq!(one(b"\r\r"), p(b"\r\r", b"")); }
#[test] fn bare_lf_passthrough() { assert_eq!(one(b"a\nb"), p(b"a\nb", b"")); }
#[test] fn cr_then_iac_negotiation() { assert_eq!(one(&cat(&[b"\r", &[IAC, WILL, ECHO]])), p(b"\r", &[IAC, DONT, ECHO])); }
/// 06 §8.4 注意：CR 後的 `IAC IAC` 是資料 0xFF，不被當作 CR 的後綴吞掉。
#[test] fn cr_then_iac_iac_is_cr_ff() { assert_eq!(one(&cat(&[b"\r", &[IAC, IAC]])), p(b"\r\xff", b"")); }
#[test] fn do_acks_of_greeting_get_no_reply_echo() { assert_eq!(one(&[IAC, DO, ECHO]), p(b"", b"")); }
#[test] fn do_acks_of_greeting_get_no_reply_sga() { assert_eq!(one(&[IAC, DO, SGA]), p(b"", b"")); }
#[test] fn do_acks_of_greeting_get_no_reply_binary() { assert_eq!(one(&[IAC, DO, BIN]), p(b"", b"")); }
#[test] fn do_unsupported_option_gets_wont() { assert_eq!(one(&[IAC, DO, TTYPE]), p(b"", &[IAC, WONT, TTYPE])); }
#[test] fn will_sga_ack_gets_no_reply() { assert_eq!(one(&[IAC, WILL, SGA]), p(b"", b"")); }
#[test] fn will_binary_refused_to_keep_nvt_inbound() { assert_eq!(one(&[IAC, WILL, BIN]), p(b"", &[IAC, DONT, BIN])); }
#[test] fn unsolicited_will_gets_dont_naws() { assert_eq!(one(&[IAC, WILL, NAWS]), p(b"", &[IAC, DONT, NAWS])); }
#[test] fn unsolicited_will_gets_dont_ttype() { assert_eq!(one(&[IAC, WILL, TTYPE]), p(b"", &[IAC, DONT, TTYPE])); }
#[test] fn unsolicited_will_gets_dont_echo() { assert_eq!(one(&[IAC, WILL, ECHO]), p(b"", &[IAC, DONT, ECHO])); }
#[test] fn wont_gets_no_reply() { assert_eq!(one(&[IAC, WONT, NAWS]), p(b"", b"")); }
#[test] fn putty_opening_burst() {
    assert_eq!(one(&[IAC, WILL, NAWS, IAC, WILL, TTYPE, IAC, DO, ECHO]), p(b"", &[IAC, DONT, NAWS, IAC, DONT, TTYPE]));
}
#[test] fn negotiation_split_across_feeds() {
    assert_eq!(feeds(&[&[IAC], &[WILL], &[NAWS]]), vec![p(b"", b""), p(b"", b""), p(b"", &[IAC, DONT, NAWS])]);
}
#[test] fn sb_swallowed_entirely() {
    assert_eq!(one(&cat(&[&[IAC, SB, NAWS, 0, 0x50, 0, 0x18, IAC, SE], b"after"])), p(b"after", b""));
}
#[test] fn sb_spanning_three_reads() {
    assert_eq!(
        feeds(&[&[IAC, SB, NAWS], &[0, 0x50, 0, 0x18], &cat(&[&[IAC, SE], b"after"])]),
        vec![p(b"", b""), p(b"", b""), p(b"after", b"")]
    );
}
#[test] fn sb_with_escaped_iac_inside() {
    assert_eq!(one(&cat(&[&[IAC, SB, TTYPE, IAC, IAC, 0x01, IAC, SE], b"x"])), p(b"x", b""));
}
#[test] fn nop_and_ayt_swallowed() { assert_eq!(one(&cat(&[&[IAC, 0xF1, IAC, 0xF6], b"ok"])), p(b"ok", b"")); }
/// IP（Interrupt Process）不轉 0x03。
#[test] fn ip_is_not_ctrl_c() { assert_eq!(one(&[IAC, 0xF4]), p(b"", b"")); }
#[test] fn interleaved_data_negotiation_cr() {
    assert_eq!(one(&cat(&[b"a", &[IAC, DO, SGA], b"b\r\x00c"])), p(b"ab\rc", b""));
}
#[test] fn escapes_ff() { assert_eq!(escape_iac(&[0xFF, 0x01, 0xFF]).into_owned(), vec![0xFF, 0xFF, 0x01, 0xFF, 0xFF]); }
#[test] fn dense_ff_buffer() { assert_eq!(escape_iac(&[0xFF; 4]).into_owned(), vec![0xFF; 8]); }
#[test] fn per_client_state_isolated() {
    let mut a = TelnetFilter::new();
    let mut b = TelnetFilter::new();
    assert_eq!(a.feed(&[IAC]), f(b"", b""));
    assert_eq!(b.feed(b"z"), f(b"z", b""));
    assert_eq!(a.feed(&[IAC]), f(&[0xFF], b""));
}

/// parity-amendments-2026-10-26 #181（P6 審查 F8）：`SW_TEST_SEED=0` 時 xorshift 狀態恆為 0、`below()` 恆回 0，
/// chunking_is_invisible 會退化成只測空輸入；建構時必須避開全零狀態。
#[test] fn xorshift_zero_seed_does_not_stall() {
    let mut rng = XorShift::new(0);
    let vals: Vec<u64> = (0..8).map(|_| rng.next()).collect();
    assert!(vals.iter().all(|&v| v != 0), "seed 0 不得產生全零序列：{vals:?}");
    let mut uniq = vals.clone(); uniq.sort_unstable(); uniq.dedup();
    assert_eq!(uniq.len(), vals.len(), "序列不得停滯：{vals:?}");
    let mut rng = XorShift::new(0);
    assert!((0..64).any(|_| rng.below(64) != 0), "below() 不得恆為 0");
}
#[test] fn xorshift_nonzero_seed_is_reproducible() {
    let a: Vec<u64> = { let mut r = XorShift::new(0x5EED); (0..4).map(|_| r.next()).collect() };
    let b: Vec<u64> = { let mut r = XorShift::new(0x5EED); (0..4).map(|_| r.next()).collect() };
    assert_eq!(a, b);
}
