//! sw-cli 會輸出的錯誤碼型別強制、源碼常值掃描與文件對齊測試（P3-T8、OD-06）。
mod common;

use std::fs;
use std::path::{Path, PathBuf};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::PyDict;

/// 單次詞法掃描的結果（OD-06 常值掃描與 ErrorCode 抽取共用同一詞法，判斷註解、字串、lifetime 的規則不可分岔）。
struct LexedSource {
    /// 去除註解與字串內容後的程式碼；註解與字串各以單一空白代替，避免相鄰 token 黏連。
    code: String,
    /// 字串常值（起始行號, 內容）；一般字串與 raw 字串皆收錄。
    literals: Vec<(usize, String)>,
}

/// 詞法掃描：辨識行註解、巢狀區塊註解、raw 字串、一般字串與字元常值。
///
/// 字元常值只認兩種形式：跳脫形（`'\n'`、`'\''`、`'\u{..}'`）與單字元形（`'x'`，含 `'"'`）。
/// 其餘單引號視為 lifetime 或 label（`'a`、`'static`），只越過 `'` 本身，不吞掉後續內容；
/// 否則 `&'static str` 之後的字串常值會被漏掃（false negative）。
fn lex_rust_source(source: &str) -> LexedSource {
    let chars: Vec<char> = source.chars().collect();
    let n = chars.len();
    let mut code = String::new();
    let mut literals = Vec::new();
    let mut i = 0;
    let mut line_num = 1;

    while i < n {
        let c = chars[i];
        if c == '\n' {
            line_num += 1;
            code.push('\n');
            i += 1;
            continue;
        }

        // 行註解 //
        if c == '/' && i + 1 < n && chars[i + 1] == '/' {
            i += 2;
            while i < n && chars[i] != '\n' {
                i += 1;
            }
            code.push(' ');
            continue;
        }

        // 區塊註解 /* ... */（支援 Rust 巢狀區塊註解）
        if c == '/' && i + 1 < n && chars[i + 1] == '*' {
            i += 2;
            let mut depth = 1;
            while i < n && depth > 0 {
                if chars[i] == '\n' {
                    line_num += 1;
                }
                if chars[i] == '/' && i + 1 < n && chars[i + 1] == '*' {
                    depth += 1;
                    i += 2;
                    continue;
                }
                if chars[i] == '*' && i + 1 < n && chars[i + 1] == '/' {
                    depth -= 1;
                    i += 2;
                    continue;
                }
                i += 1;
            }
            code.push(' ');
            continue;
        }

        // raw 字串 r"..." 或 r#"..."#
        if c == 'r' && i + 1 < n && (chars[i + 1] == '"' || chars[i + 1] == '#') {
            let start_line = line_num;
            let mut hash_count = 0;
            let mut j = i + 1;
            while j < n && chars[j] == '#' {
                hash_count += 1;
                j += 1;
            }
            if j < n && chars[j] == '"' {
                j += 1;
                let mut content = String::new();
                let mut closed = false;
                while j < n {
                    if chars[j] == '\n' {
                        line_num += 1;
                    }
                    if chars[j] == '"' {
                        let mut k = 0;
                        while j + 1 + k < n && k < hash_count && chars[j + 1 + k] == '#' {
                            k += 1;
                        }
                        if k == hash_count {
                            j += 1 + hash_count;
                            closed = true;
                            break;
                        }
                    }
                    content.push(chars[j]);
                    j += 1;
                }
                if closed {
                    literals.push((start_line, content));
                    code.push(' ');
                    i = j;
                    continue;
                }
            }
        }

        // 一般字串 "..."
        if c == '"' {
            let start_line = line_num;
            i += 1;
            let mut content = String::new();
            let mut escaped = false;
            while i < n {
                let sc = chars[i];
                if sc == '\n' {
                    line_num += 1;
                }
                if escaped {
                    content.push(sc);
                    escaped = false;
                } else if sc == '\\' {
                    escaped = true;
                } else if sc == '"' {
                    i += 1;
                    break;
                } else {
                    content.push(sc);
                }
                i += 1;
            }
            literals.push((start_line, content));
            code.push(' ');
            continue;
        }

        // 字元常值 '...' 或 lifetime／label
        if c == '\'' {
            // 跳脫形：'\n'、'\''、'\u{..}'，越過 ' 與 \ 與被跳脫的字元後，找結尾 '
            if i + 1 < n && chars[i + 1] == '\\' {
                i += 3;
                while i < n && chars[i] != '\'' {
                    if chars[i] == '\n' {
                        line_num += 1;
                    }
                    i += 1;
                }
                i += 1;
                code.push(' ');
                continue;
            }
            // 單字元形：'x'（含 '"'，使其內的雙引號不被誤認為字串起點）
            if i + 2 < n && chars[i + 2] == '\'' {
                i += 3;
                code.push(' ');
                continue;
            }
            // lifetime／label（'a、'static）：只越過 '，不吞掉後續內容
            code.push('\'');
            i += 1;
            continue;
        }

        code.push(c);
        i += 1;
    }

    LexedSource { code, literals }
}

/// 剝除註解並提取所有字串常值（含一般字串 "..." 與 raw 字串 r"..." / r#"..."#）。
fn strip_comments_and_extract_string_literals(source: &str) -> Vec<(usize, String)> {
    lex_rust_source(source).literals
}

/// 判定字串常值是否符合 "[A-Z][A-Z0-9_]{3,}" 且不在允許清單內。
fn is_forbidden_code_literal(s: &str) -> bool {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() < 4 {
        return false;
    }
    if !chars[0].is_ascii_uppercase() {
        return false;
    }
    if !chars.iter().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == '_') {
        return false;
    }
    // 允許清單：SERIALWRAP_*, SW_*, UNKNOWN_ERROR
    if s.starts_with("SERIALWRAP_") || s.starts_with("SW_") || s == "UNKNOWN_ERROR" {
        return false;
    }
    true
}

/// 遞迴收集目錄下所有 .rs 檔案路徑。
fn collect_rs_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    if dir.is_dir() {
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.is_dir() {
                    files.extend(collect_rs_files(&p));
                } else if p.extension().map_or(false, |ext| ext == "rs") {
                    files.push(p);
                }
            }
        }
    }
    files.sort();
    files
}

/// 掃描指定目錄下所有 .rs 檔案是否有違規字串常值。
fn scan_forbidden_literals(dir: &Path) -> Vec<(PathBuf, usize, String)> {
    let mut violations = Vec::new();
    for file in collect_rs_files(dir) {
        if let Ok(content) = fs::read_to_string(&file) {
            let literals = strip_comments_and_extract_string_literals(&content);
            for (line, lit) in literals {
                if is_forbidden_code_literal(&lit) {
                    violations.push((file.clone(), line, lit));
                }
            }
        }
    }
    violations
}

/// 將 UpperCamel（例如 SocketError）轉回 UPPER_SNAKE（例如 SOCKET_ERROR）。
fn camel_to_upper_snake(s: &str) -> String {
    let mut out = String::new();
    let chars: Vec<char> = s.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if c.is_ascii_uppercase() {
            if i > 0 {
                let prev = chars[i - 1];
                let next = chars.get(i + 1);
                let prev_not_upper = !prev.is_ascii_uppercase();
                let next_is_lower = next.map(|n| n.is_ascii_lowercase()).unwrap_or(false);
                if prev_not_upper || (prev.is_ascii_uppercase() && next_is_lower) {
                    out.push('_');
                }
            }
            out.push(c);
        } else {
            out.push(c.to_ascii_uppercase());
        }
    }
    out
}

/// 剝除註解與字串內容後，提取 ErrorCode::([A-Za-z0-9]+) 的所有使用處（與常值掃描共用同一詞法）。
fn extract_error_code_variants(source: &str) -> Vec<String> {
    let clean_src = lex_rust_source(source).code;

    let mut variants = Vec::new();
    let pattern = "ErrorCode::";
    let mut rest = clean_src.as_str();
    while let Some(idx) = rest.find(pattern) {
        let after = &rest[idx + pattern.len()..];
        let ident: String = after
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
            .collect();
        if !ident.is_empty() && ident.chars().next().unwrap().is_ascii_alphabetic() {
            variants.push(ident);
        }
        rest = after;
    }
    variants
}

/// 掃描指定目錄下所有 .rs 檔案，提取所有 ErrorCode 使用處並轉為 UPPER_SNAKE。
fn extract_emitted_codes_from_dir(dir: &Path) -> Vec<String> {
    let mut codes = Vec::new();
    for file in collect_rs_files(dir) {
        if let Ok(content) = fs::read_to_string(&file) {
            for v in extract_error_code_variants(&content) {
                codes.push(camel_to_upper_snake(&v));
            }
        }
    }
    codes.sort();
    codes.dedup();
    codes
}

/// 驗證單一錯誤碼是否在 error_codes.yaml 或 SKILL.md 中有說明。
fn is_code_documented(code: &str, yaml_text: &str, skill_text: &str) -> bool {
    // 1. contracts/error_codes.yaml：尋找 code: <code_name> 及其後的 meaning / doc
    let code_pat = format!("code: {code}");
    if let Some(pos) = yaml_text.find(&code_pat) {
        let slice = &yaml_text[pos..std::cmp::min(pos + 1000, yaml_text.len())];
        if slice.contains("meaning:") || slice.contains("doc:") {
            return true;
        }
    }
    // 2. sw_core/assets/skill/SKILL.md：尋找表格中的標記
    let skill_pat1 = format!("`{code}`");
    let skill_pat2 = format!("| {code} |");
    if skill_text.contains(&skill_pat1) || skill_text.contains(&skill_pat2) {
        return true;
    }
    false
}

#[test]
fn error_resp_type_enforcement_and_execution() {
    // 1. 型別強制：sw_cli::output::error_resp 簽名必須只接受 ErrorCode，不收 &str
    let f: fn(ErrorCode, Option<&str>) -> PyDict = sw_cli::output::error_resp;
    let _ = f;

    // 執行期呼叫：在 red stage 呼叫 todo!() 會 panic 失敗
    let code = ErrorCode::parse("INVALID_ENDPOINT").expect("INVALID_ENDPOINT must parse");
    let resp = sw_cli::output::error_resp(code, Some("test message"));
    assert_eq!(resp.get("ok"), Some(&sw_proto::pyjson::PyValue::Bool(false)));
}

#[test]
fn no_forbidden_uppercase_string_literals_in_sw_cli_src() {
    // 2. 掃描 crates/sw-cli/src/**/*.rs（剝除註解後），任何符合 "[A-Z][A-Z0-9_]{3,}" 的字串常值即失敗
    let src_dir = common::data_root().join("crates").join("sw-cli").join("src");
    let violations = scan_forbidden_literals(&src_dir);
    assert!(
        violations.is_empty(),
        "發現禁止的錯誤碼字串常值（應使用 ErrorCode::*）：{violations:?}"
    );
}

#[test]
fn extracted_emitted_codes_documented_and_written_to_target() {
    // 3. 抽出 ErrorCode::([A-Za-z]+) 全部使用處，轉回 UPPER_SNAKE，輸出到 target/emitted_codes.sw-cli.txt
    let root = common::data_root();
    let src_dir = root.join("crates").join("sw-cli").join("src");
    let codes = extract_emitted_codes_from_dir(&src_dir);

    let target_dir = root.join("target");
    let _ = fs::create_dir_all(&target_dir);
    let out_file = target_dir.join("emitted_codes.sw-cli.txt");
    let mut out_text = codes.join("\n");
    if !out_text.is_empty() {
        out_text.push('\n');
    }
    fs::write(&out_file, &out_text).expect("寫入 target/emitted_codes.sw-cli.txt 失敗");

    let yaml_p = root.join("contracts").join("error_codes.yaml");
    let skill_p = root.join("sw_core").join("assets").join("skill").join("SKILL.md");
    let yaml_text = fs::read_to_string(&yaml_p).unwrap_or_default();
    let skill_text = fs::read_to_string(&skill_p).unwrap_or_default();

    for c in &codes {
        assert!(
            is_code_documented(c, &yaml_text, &skill_text),
            "錯誤碼 {c} 在 contracts/error_codes.yaml 或 SKILL.md 中缺少說明文件"
        );
    }
}

#[test]
fn negative_self_test_unknown_code_string_literal_fails() {
    // 4. 負向自測："NEW_UNKNOWN_CODE" 常值 → FAIL
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(&file, r#"fn foo() { let _ = "NEW_UNKNOWN_CODE"; }"#).unwrap();
    let violations = scan_forbidden_literals(dir.path());
    assert_eq!(violations.len(), 1);
    assert_eq!(violations[0].2, "NEW_UNKNOWN_CODE");
}

#[test]
fn negative_self_test_const_string_forwarding_fails() {
    // 4. 負向自測：const C: &str = "SOCKET_ERROR"; 常數轉傳 → FAIL
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(&file, r#"const C: &str = "SOCKET_ERROR";"#).unwrap();
    let violations = scan_forbidden_literals(dir.path());
    assert_eq!(violations.len(), 1);
    assert_eq!(violations[0].2, "SOCKET_ERROR");
}

#[test]
fn negative_self_test_undocumented_error_code_fails() {
    // 4. 負向自測：ErrorCode::SocketError 使用但文件缺說明 → FAIL
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(&file, r#"fn foo() { let _ = ErrorCode::SocketError; }"#).unwrap();
    let codes = extract_emitted_codes_from_dir(dir.path());
    assert_eq!(codes, vec!["SOCKET_ERROR"]);

    // 給定空的說明文件，斷言判定為未記載
    assert!(
        !is_code_documented(&codes[0], "", ""),
        "無說明之文件應判定為 FAIL"
    );
}

#[test]
fn negative_self_test_lifetime_does_not_hide_following_string_literal() {
    // 4. 負向自測（OD-06）：&'static 之後的字串常值必須被掃到（舊詞法會整段吞掉）
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(&file, r#"fn name() -> &'static str { "TIMEOUT" }"#).unwrap();
    let violations = scan_forbidden_literals(dir.path());
    assert_eq!(violations.len(), 1);
    assert_eq!(violations[0].2, "TIMEOUT");
}

#[test]
fn negative_self_test_char_literal_quote_does_not_open_string() {
    // 4. 負向自測（OD-06）：'"' 與 '\'' 字元常值不得開啟字串，其後的常值仍須掃到
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(
        &file,
        r#"fn f() { let _q = '"'; let _e = '\''; let _s = "SOCKET_ERROR"; }"#,
    )
    .unwrap();
    let violations = scan_forbidden_literals(dir.path());
    assert_eq!(violations.len(), 1);
    assert_eq!(violations[0].2, "SOCKET_ERROR");
}

#[test]
fn negative_self_test_line_comment_marker_inside_string_does_not_hide_code() {
    // 4. 負向自測（OD-06）："unix://" 內的 // 不是行註解，其後的 ErrorCode 使用處仍須抽出
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(
        &file,
        r#"fn f() { let _ = Err(E::new("unix://", ErrorCode::InvalidEndpoint)); }"#,
    )
    .unwrap();
    let codes = extract_emitted_codes_from_dir(dir.path());
    assert_eq!(codes, vec!["INVALID_ENDPOINT"]);
}

#[test]
fn negative_self_test_block_comment_marker_inside_string_does_not_hide_code() {
    // 4. 負向自測（OD-06）：字串 "/*" 不得開啟區塊註解，否則其後的程式碼會被整段吞掉
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(
        &file,
        r#"fn f() { let _ = ("/*", ErrorCode::SocketError); }"#,
    )
    .unwrap();
    let codes = extract_emitted_codes_from_dir(dir.path());
    assert_eq!(codes, vec!["SOCKET_ERROR"]);
}

#[test]
fn negative_self_test_lifetime_does_not_hide_error_code_use() {
    // 4. 負向自測（OD-06）：lifetime 之後的 ErrorCode 使用處仍須抽出
    let dir = tempfile::tempdir().expect("tempdir");
    let file = dir.path().join("dummy.rs");
    fs::write(
        &file,
        r#"fn f() -> &'static str { let _ = ErrorCode::SocketError; "" }"#,
    )
    .unwrap();
    let codes = extract_emitted_codes_from_dir(dir.path());
    assert_eq!(codes, vec!["SOCKET_ERROR"]);
}
