//! P6 Task 7：ATTACHED 救援判定（`rescue.rs`；自 06 T14 抽出，行為不變）。
#![cfg(feature = "test-fakes")]
mod support;

use support::profile_of;
use sw_core::rescue::*;

const P: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    bootloader_prompts: ['^=> $', '^U-Boot> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";
const NO_PROMPTS: &str = "profiles:\n  n:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    bootloader_prompts: []\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: n, act_no: 1, alias: n+1, device_by_id: /s/by-id/usb-A}\n";

fn pats(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

#[test]
fn bootloader_prompt_uses_last_non_blank_line() {
    assert_eq!(
        matches_any_bootloader_prompt("boot\n=> \n  \n", &pats(&["^=> $"])),
        Some("^=> $".to_string())
    );
    assert_eq!(matches_any_bootloader_prompt("=> \nroot# ", &pats(&["^=> $"])), None, "只看最後一行");
}

#[test]
fn bootloader_prompt_returns_first_matching_pattern() {
    assert_eq!(
        matches_any_bootloader_prompt("U-Boot> ", &pats(&["^=> $", "^U-Boot> $", "U-Boot"])),
        Some("^U-Boot> $".to_string())
    );
}

#[test]
fn bootloader_prompt_invalid_regex_skipped() {
    assert_eq!(
        matches_any_bootloader_prompt("=> ", &pats(&["(", "^=> $"])),
        Some("^=> $".to_string())
    );
}

#[test]
fn bootloader_prompt_empty_input_none() {
    assert_eq!(matches_any_bootloader_prompt("", &pats(&["^=> $"])), None);
    assert_eq!(matches_any_bootloader_prompt("=> ", &[]), None);
    assert_eq!(matches_any_bootloader_prompt("\n  \n", &pats(&["^=> $"])), None);
}

#[test]
fn login_cleans_before_match() {
    assert!(matches_login_or_password("\x1b[1m(none) login:\x1b[0m ", "login: ?$", "Password: ?$"));
}

#[test]
fn login_raw_or_cleaned_either_matches() {
    // 原文含控制碼時以原文比對命中（cleaned 去掉 \x07 後不符）
    assert!(matches_login_or_password("pw\x07", "\\x07", ""));
    assert!(matches_login_or_password("Password: ", "login: ?$", "Password: ?$"));
    assert!(!matches_login_or_password("shell# ", "login: ?$", "Password: ?$"));
}

#[test]
fn login_invalid_regex_skipped_and_empty() {
    assert!(matches_login_or_password("login: ", "(", "login: ?$"));
    assert!(!matches_login_or_password("", "login: ?$", "Password: ?$"));
    assert!(!matches_login_or_password("login: ", "", ""));
}

#[test]
fn classify_bootloader_prompt() {
    let p = profile_of(P);
    assert_eq!(classify_attached_rescue("U-Boot 2020\r\n=> ", &p), RescueClass::BootloaderPrompt);
    assert_eq!(classify_attached_rescue("login: \r\n=> ", &p), RescueClass::BootloaderPrompt, "bootloader 優先於 login");
}

#[test]
fn classify_boot_interrupt() {
    let p = profile_of(P);
    assert_eq!(classify_attached_rescue("Hit any key to stop autoboot:  3", &p), RescueClass::BootInterrupt);
    assert_eq!(
        classify_attached_rescue("Hit any key to stop autoboot:  2\r\nlogin: ", &p),
        RescueClass::BootInterrupt,
        "banner 優先於 login"
    );
}

#[test]
fn classify_login_required() {
    let p = profile_of(P);
    assert_eq!(classify_attached_rescue("\x1b[1m(none) login:\x1b[0m ", &p), RescueClass::LoginRequired);
    assert_eq!(classify_attached_rescue("Password: ", &p), RescueClass::LoginRequired);
}

#[test]
fn classify_not_bootloader() {
    let p = profile_of(P);
    assert_eq!(classify_attached_rescue("random text", &p), RescueClass::NotBootloader);
    let n = profile_of(NO_PROMPTS);
    assert_eq!(classify_attached_rescue("=> ", &n), RescueClass::NotBootloader, "不用 UBOOT_FALLBACK_PROMPTS");
}
