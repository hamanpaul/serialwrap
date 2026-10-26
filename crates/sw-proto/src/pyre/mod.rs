//! Python re 相容層。
//!
//! 語法接受集合、re.error 訊息與 code point 位置依 CPython 3.12.3；執行引擎為 fancy-regex（ADR-01）。

mod parse;
pub mod redos;
mod translate;
mod unicode_tables;

use fancy_regex::{Regex, RegexBuilder};

use crate::pycompat::{byte_index_to_char, char_index_to_byte, py_len_chars};
use crate::pyjson::PyValue;

/// Python re 旗標位元（與 CPython re 常數同值）。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PyFlags(pub u32);

impl PyFlags {
    pub const T: u32 = 1;
    pub const I: u32 = 2;
    pub const L: u32 = 4;
    pub const M: u32 = 8;
    pub const S: u32 = 16;
    pub const U: u32 = 32;
    pub const X: u32 = 64;
    pub const A: u32 = 256;
}

/// 對應 Python re.error。
///
/// 有位置時附上位置；pattern 含換行時再附 Python 的行號與欄號。
#[derive(Debug, Clone, PartialEq)]
pub struct PyReError {
    pub msg: String,
    pub pos: Option<usize>,
    pub line_col: Option<(usize, usize)>,
    /// True 表示 CPython `re.compile` 會拋出 ValueError，而非 re.error。
    pub value_error: bool,
}

impl std::fmt::Display for PyReError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.msg)?;
        if let Some(pos) = self.pos {
            write!(f, " at position {pos}")?;
        }
        if let Some((line, column)) = self.line_col {
            write!(f, " (line {line}, column {column})")?;
        }
        Ok(())
    }
}

/// 編譯錯誤分類。
///
/// Re 對應 Python re.error；Recursion 對應 RecursionError；Overflow 對應
/// OverflowError；Untranslatable 表示語法有效但執行引擎不能忠實執行。
#[derive(Debug, Clone, PartialEq)]
pub enum CompileError {
    Re(PyReError),
    Recursion,
    Overflow(String),
    Untranslatable(String),
}

/// 已編譯的 Python re pattern。
pub struct PyRegex {
    _pattern: String,
    regex: Option<Regex>,
    large_literal_repeat: Option<LargeLiteralRepeat>,
}

#[derive(Clone)]
struct LargeLiteralRepeat {
    literal: char,
    min: usize,
    max: Option<usize>,
    mode: parse::RepeatMode,
    ignore_case: bool,
    ascii: bool,
}

/// 匹配區間；索引單位為 Unicode code point。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PyMatch {
    pub start: usize,
    pub end: usize,
}

/// 執行期回溯上限；超過時視為未匹配。
pub const BACKTRACK_LIMIT: usize = 1_000_000;

/// Python re.compile 的巢狀群組 RecursionError 起始深度（CPython 3.12.3）。
pub const PARSE_DEPTH_LIMIT: usize = 495;

pub(super) const ENGINE_REPEAT_LIMIT: usize = 100_000;

/// completion regex 的 UTF-8 長度上限。
const COMPLETION_REGEX_MAX_BYTES: usize = 1024;

/// completion regex 驗證結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompletionCheck {
    Valid,
    Invalid,
    /// Python 未攔截的 OverflowError；CLI 呼叫端須回傳 exit 1 且不輸出 stdout。
    Crash(String),
}

impl PyRegex {
    /// 自字串起點搜尋並回傳 code point 索引。
    pub fn search(&self, hay: &str) -> Option<PyMatch> {
        if let Some(repeat) = &self.large_literal_repeat {
            return repeat.search(hay, 0);
        }
        let found = self.regex.as_ref()?.find(hay).ok().flatten()?;
        Some(PyMatch {
            start: byte_index_to_char(hay, found.start()),
            end: byte_index_to_char(hay, found.end()),
        })
    }

    /// 自 Python 字元索引 pos 開始搜尋。
    pub fn search_from(&self, hay: &str, pos: usize) -> Option<PyMatch> {
        let byte_pos = char_index_to_byte(hay, pos.min(py_len_chars(hay)));
        if let Some(repeat) = &self.large_literal_repeat {
            return repeat.search(hay, byte_index_to_char(hay, byte_pos));
        }
        let found = self
            .regex
            .as_ref()?
            .find_from_pos(hay, byte_pos)
            .ok()
            .flatten()?;
        Some(PyMatch {
            start: byte_index_to_char(hay, found.start()),
            end: byte_index_to_char(hay, found.end()),
        })
    }
}

impl LargeLiteralRepeat {
    fn search(&self, hay: &str, pos: usize) -> Option<PyMatch> {
        let chars: Vec<char> = hay.chars().collect();
        let mut index = pos.min(chars.len());
        if self.min == 0 && matches!(self.mode, parse::RepeatMode::Lazy) {
            return Some(PyMatch {
                start: index,
                end: index,
            });
        }
        while index <= chars.len() {
            if index == chars.len() || !self.matches(chars[index]) {
                if self.min == 0 {
                    return Some(PyMatch {
                        start: index,
                        end: index,
                    });
                }
                if index == chars.len() {
                    break;
                }
                index += 1;
                continue;
            }
            let start = index;
            while index < chars.len() && self.matches(chars[index]) {
                index += 1;
            }
            let run_length = index - start;
            if run_length < self.min {
                continue;
            }
            let matched = if matches!(self.mode, parse::RepeatMode::Lazy) {
                self.min
            } else {
                self.max.map_or(run_length, |max| run_length.min(max))
            };
            return Some(PyMatch {
                start,
                end: start + matched,
            });
        }
        None
    }

    fn matches(&self, candidate: char) -> bool {
        if !self.ignore_case || candidate == self.literal {
            return candidate == self.literal;
        }
        if self.ascii {
            return self.literal.is_ascii_alphabetic()
                && candidate.is_ascii_alphabetic()
                && candidate.eq_ignore_ascii_case(&self.literal);
        }
        unicode_tables::case_group(self.literal as u32)
            .is_some_and(|members| members.contains(&(candidate as u32)))
    }
}

/// CPython 3.7+ `re._special_chars_map` 的跳脫字元集合（`re.escape` 只在這些字元前加反斜線）。
const ESCAPE_SPECIAL: &[char] = &[
    '(', ')', '[', ']', '{', '}', '?', '*', '+', '-', '|', '^', '$', '\\', '.', '&', '~', '#', ' ', '\t', '\n', '\r',
    '\x0b', '\x0c',
];

/// 對應 Python `re.escape(str)`（CPython 3.12）：僅跳脫 `_special_chars_map` 內的 ASCII 字元，
/// 其餘（含 NUL、非 ASCII）原樣保留。
pub fn escape(pattern: &str) -> String {
    let mut out = String::with_capacity(pattern.len());
    for ch in pattern.chars() {
        if ESCAPE_SPECIAL.contains(&ch) {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

/// 以 Python re 語法接受集合編譯 pattern。
pub fn compile(pat: &str, flags: PyFlags) -> Result<PyRegex, CompileError> {
    if estimated_group_depth(pat) > 64 {
        let pattern = pat.to_string();
        let worker = std::thread::Builder::new()
            .name("sw-proto-pyre-deep-parse".to_string())
            .stack_size(16 * 1024 * 1024)
            .spawn(move || compile_inner(&pattern, flags))
            .unwrap_or_else(|error| panic!("無法啟動 pyre parser 執行緒：{error}"));
        return match worker.join() {
            Ok(result) => result,
            Err(payload) => std::panic::resume_unwind(payload),
        };
    }
    compile_inner(pat, flags)
}

fn compile_inner(pat: &str, flags: PyFlags) -> Result<PyRegex, CompileError> {
    let parsed = parse::parse(pat, flags)?;
    if let parse::Expr::Repeat {
        body,
        min,
        max,
        mode,
    } = &parsed.expression
    {
        if let parse::Expr::Literal(literal) = **body {
            if *min > ENGINE_REPEAT_LIMIT || max.is_some_and(|count| count > ENGINE_REPEAT_LIMIT) {
                return Ok(PyRegex {
                    _pattern: pat.to_string(),
                    regex: None,
                    large_literal_repeat: Some(LargeLiteralRepeat {
                        literal,
                        min: *min,
                        max: *max,
                        mode: mode.clone(),
                        ignore_case: parsed.global_flags & PyFlags::I != 0,
                        ascii: parsed.global_flags & PyFlags::A != 0,
                    }),
                });
            }
        }
    }
    let translated = translate::translate(&parsed).map_err(CompileError::Untranslatable)?;
    let mut builder = RegexBuilder::new(&translated);
    builder.backtrack_limit(BACKTRACK_LIMIT);
    let regex = builder
        .build()
        .map_err(|error| CompileError::Untranslatable(format!("fancy-regex:{error}")))?;
    Ok(PyRegex {
        _pattern: pat.to_string(),
        regex: Some(regex),
        large_literal_repeat: None,
    })
}

fn estimated_group_depth(pattern: &str) -> usize {
    let mut depth = 0usize;
    let mut maximum = 0usize;
    let mut escaped = false;
    let mut in_class = false;
    for ch in pattern.chars() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if in_class {
            if ch == ']' {
                in_class = false;
            }
            continue;
        }
        match ch {
            '[' => in_class = true,
            '(' => {
                depth += 1;
                maximum = maximum.max(depth);
            }
            ')' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    maximum
}

/// completion regex 驗證，對應 command_completion.valid_completion_regex。
///
/// None 有效；非字串、空字串與超長字串無效；re.error／RecursionError 無效；
/// OverflowError 交由 CLI 回報；引擎未支援的 Python 語法交由 daemon 驗證。
pub fn completion_regex_check(v: Option<&PyValue>) -> CompletionCheck {
    completion_regex_check_mode(v, true)
}

/// daemon 端使用的嚴格驗證；無法忠實轉譯的 regex 一律不通過。
pub fn completion_regex_check_strict(v: Option<&PyValue>) -> CompletionCheck {
    completion_regex_check_mode(v, false)
}

fn completion_regex_check_mode(v: Option<&PyValue>, allow_untranslatable: bool) -> CompletionCheck {
    let Some(PyValue::Str(pattern)) = v else {
        return if v.is_none() {
            CompletionCheck::Valid
        } else {
            CompletionCheck::Invalid
        };
    };
    if pattern.is_empty() || pattern.len() > COMPLETION_REGEX_MAX_BYTES {
        return CompletionCheck::Invalid;
    }
    match compile(pattern, PyFlags::default()) {
        Ok(regex) => {
            if regex.search("").is_some() {
                CompletionCheck::Invalid
            } else {
                CompletionCheck::Valid
            }
        }
        Err(CompileError::Re(error)) if error.value_error => {
            CompletionCheck::Crash(error.msg.clone())
        }
        Err(CompileError::Re(_)) | Err(CompileError::Recursion) => CompletionCheck::Invalid,
        Err(CompileError::Overflow(message)) => CompletionCheck::Crash(message),
        Err(CompileError::Untranslatable(_)) if allow_untranslatable => CompletionCheck::Valid,
        Err(CompileError::Untranslatable(_)) => CompletionCheck::Invalid,
    }
}

/// completion_regex_check(v) 是否為 Valid。
pub fn valid_completion_regex(v: Option<&PyValue>) -> bool {
    matches!(completion_regex_check(v), CompletionCheck::Valid)
}

/// strict completion_regex_check(v) 是否為 Valid。
pub fn valid_completion_regex_strict(v: Option<&PyValue>) -> bool {
    matches!(completion_regex_check_strict(v), CompletionCheck::Valid)
}

#[cfg(test)]
mod review_regression_tests {
    use super::{
        compile, completion_regex_check, CompileError, CompletionCheck, PyFlags, PARSE_DEPTH_LIMIT,
    };

    fn re_error(pattern: &str) -> String {
        match compile(pattern, PyFlags::default()) {
            Err(CompileError::Re(error)) => error.to_string(),
            Err(other) => panic!("預期 re.error，得到 {other:?}"),
            Ok(_) => panic!("預期 re.error，但 pattern 編譯成功：{pattern:?}"),
        }
    }

    #[test]
    fn only_unescaped_hyphen_forms_character_ranges() {
        let rx = compile(r"[+\-*/]", PyFlags::default()).unwrap();
        assert_eq!(rx.search("+"), Some(super::PyMatch { start: 0, end: 1 }));
        let rx = compile(r"[a\-z]", PyFlags::default()).unwrap();
        assert_eq!(rx.search("m"), None);
        assert_eq!(
            re_error(r"[a-\d]"),
            r"bad character range a-\d at position 1"
        );
    }

    #[test]
    fn empty_lower_bound_brace_repeat_is_unbounded() {
        let rx = compile("a{,}", PyFlags::default()).unwrap();
        assert_eq!(rx.search(""), Some(super::PyMatch { start: 0, end: 0 }));
    }

    #[test]
    fn zero_width_escape_atoms_cannot_be_repeated() {
        for pattern in [r"\b*", r"\B+", r"\A+", r"\Z?"] {
            assert!(
                matches!(
                    compile(pattern, PyFlags::default()),
                    Err(CompileError::Re(_))
                ),
                "{pattern}"
            );
        }
        assert_eq!(re_error(r"\B{,}c"), "nothing to repeat at position 2");
    }

    #[test]
    fn numeric_escapes_follow_python_octal_and_backreference_limits() {
        for (pattern, message) in [
            (
                r"\777",
                r"octal escape value \777 outside of range 0-0o377 at position 0",
            ),
            (
                r"\400",
                r"octal escape value \400 outside of range 0-0o377 at position 0",
            ),
            (
                r"[\400]",
                r"octal escape value \400 outside of range 0-0o377 at position 1",
            ),
        ] {
            assert_eq!(re_error(pattern), message);
        }
        let pattern = format!("{}(a)\\118", "()".repeat(10));
        let rx = compile(&pattern, PyFlags::default()).unwrap();
        assert_eq!(rx.search("aa8").map(|m| (m.start, m.end)), Some((0, 3)));
    }

    #[test]
    fn inline_flag_syntax_and_scoped_ascii_overrides_match_python() {
        assert_eq!(re_error("(?-i)a"), "missing : at position 4");
        assert_eq!(re_error("(?i-)a"), "missing flag at position 4");
        assert_eq!(re_error("(?i-s)a"), "missing : at position 5");
        assert_eq!(
            re_error("(?i-i:a)"),
            "bad inline flags: flag turned on and off at position 5"
        );
        compile("(?a:x(?u:\\w))", PyFlags::default()).unwrap();
        assert_eq!(
            completion_regex_check(Some(&"(?a)(?u)x".into())),
            CompletionCheck::Crash("ASCII and UNICODE flags are incompatible".into())
        );
    }

    #[test]
    fn conditional_groups_validate_branch_count_and_forward_references_after_parse() {
        assert_eq!(
            re_error("(a)(?(1)a|b|c)"),
            "conditional backref with more than two branches at position 11"
        );
        compile("(?(1)a|b)(c)", PyFlags::default()).unwrap();
        compile("(?(1)(a)|b)", PyFlags::default()).unwrap();
    }

    #[test]
    fn lookbehind_rejects_references_to_groups_defined_inside_it() {
        assert_eq!(
            re_error(r"(?<=(a)\1)"),
            "cannot refer to group defined in the same lookbehind subpattern at position 9"
        );
        assert_eq!(
            re_error("(?<=a+)("),
            "missing ), unterminated subpattern at position 7"
        );
    }

    #[test]
    fn group_names_use_python_identifier_and_repr_rules() {
        assert_eq!(
            re_error("(?P<a²>x)"),
            "bad character in group name 'a²' at position 4"
        );
        assert_eq!(re_error("(?P<>x)"), "missing group name at position 4");
        assert_eq!(
            re_error("(?P<a'b>x)"),
            "bad character in group name \"a'b\" at position 4"
        );
    }

    #[test]
    fn unicode_character_names_do_not_guess_and_cover_unicode_15_names() {
        for (pattern, character) in [
            (r"\N{DIGIT ONE}", "1"),
            (r"\N{EM DASH}", "—"),
            (r"\N{BULLET}", "•"),
            (r"\N{LATIN CAPITAL LETTER A}", "A"),
        ] {
            let rx = compile(pattern, PyFlags::default()).unwrap();
            assert_eq!(
                rx.search(character).map(|m| (m.start, m.end)),
                Some((0, 1)),
                "{pattern}"
            );
        }
        assert!(matches!(
            compile(r"\N{HORIZONTAL TAB}", PyFlags::default()),
            Err(CompileError::Re(_))
        ));
        assert!(matches!(
            compile(r"\N{LATIN SMALL LETTER ABC WITH ACUTE}", PyFlags::default()),
            Err(CompileError::Re(_))
        ));
    }

    #[test]
    fn comments_are_transparent_to_repeat_attachment() {
        let rx = compile("a(?#c)*", PyFlags::default()).unwrap();
        assert_eq!(rx.search(""), Some(super::PyMatch { start: 0, end: 0 }));
        assert_eq!(re_error("a*(?#c)*"), "multiple repeat at position 7");
    }

    #[test]
    fn template_flag_and_surrogates_are_not_misclassified() {
        compile("(?t)a", PyFlags::default()).unwrap();
        assert_eq!(
            re_error("(?t:x)"),
            "bad inline flags: cannot turn on global flag at position 3"
        );
        assert!(matches!(
            compile(r"\ud800", PyFlags::default()),
            Err(CompileError::Untranslatable(_))
        ));
    }

    #[test]
    fn python_category_complements_and_unicode_15_tables_are_used() {
        let rx = compile(r"[\W]", PyFlags::default()).unwrap();
        assert_eq!(rx.search("²"), None);
        let rx = compile(r"[^\W]", PyFlags::default()).unwrap();
        assert_eq!(rx.search("\u{0301}"), None);
        let rx = compile(r"(?a:[\W])", PyFlags::default()).unwrap();
        assert_eq!(rx.search("é").map(|m| (m.start, m.end)), Some((0, 1)));
        let rx = compile(r"(?a:[\D])", PyFlags::default()).unwrap();
        assert_eq!(rx.search("٤").map(|m| (m.start, m.end)), Some((0, 1)));
        let rx = compile(r"[\S]", PyFlags::default()).unwrap();
        assert_eq!(rx.search("\u{001c}"), None);
        let rx = compile(r"\w", PyFlags::default()).unwrap();
        assert_eq!(rx.search("\u{1c89}"), None);
        let rx = compile(r"\d", PyFlags::default()).unwrap();
        assert_eq!(rx.search("\u{10d40}"), None);
        assert_eq!(
            compile(r"\w", PyFlags::default()).unwrap().search("_"),
            Some(super::PyMatch { start: 0, end: 1 })
        );
    }

    #[test]
    fn ignorecase_uses_python_ascii_and_unicode_folding() {
        assert_eq!(
            compile("(?ai)É", PyFlags::default()).unwrap().search("é"),
            None
        );
        assert_eq!(
            compile("(?ia)k", PyFlags::default()).unwrap().search("K"),
            None
        );
        assert!(compile("(?i)i", PyFlags::default())
            .unwrap()
            .search("İ")
            .is_some());
        assert!(compile("(?i)ß", PyFlags::default())
            .unwrap()
            .search("ẞ")
            .is_some());
    }

    #[test]
    fn oversized_repeats_are_never_approximated() {
        let rx = compile("x{100001}", PyFlags::default()).unwrap();
        let hay = "x".repeat(100_002);
        assert_eq!(
            rx.search(&hay).map(|m| (m.start, m.end)),
            Some((0, 100_001))
        );
    }

    #[test]
    fn possessive_repeats_do_not_backtrack_between_iterations() {
        let rx = compile("(a.{,2}){2}+", PyFlags::default()).unwrap();
        assert_eq!(rx.search("aab"), None);
    }

    #[test]
    fn possessive_repeat_boundaries_match_python_for_simple_alternatives() {
        let rx = compile("(?:a|ab){1}+c", PyFlags::default()).unwrap();
        assert_eq!(rx.search("abc"), None);
    }

    #[test]
    fn scoped_flags_keep_their_group_boundary() {
        let rx = compile("x(?a:a|b)y", PyFlags::default()).unwrap();
        assert_eq!(rx.search("xa"), None);
    }

    #[test]
    fn search_from_clamps_positions_to_python_string_length() {
        let rx = compile("$", PyFlags::default()).unwrap();
        assert_eq!(
            rx.search_from("abc", 10).map(|m| (m.start, m.end)),
            Some((3, 3))
        );
    }

    #[test]
    fn oversized_empty_capable_repeat_uses_cli_and_daemon_validation_contracts() {
        assert_eq!(
            completion_regex_check(Some(&"(?:x*){100001}".into())),
            CompletionCheck::Valid
        );
        assert_eq!(
            super::completion_regex_check_strict(Some(&"(?:x*){100001}".into())),
            CompletionCheck::Invalid
        );
    }

    #[test]
    fn rust_parse_depth_matches_python_oracle_measurement() {
        let text = include_str!("../../../../contracts/golden/pyre.limits.json");
        let (_, value) = text
            .split_once("\"parse_depth_limit\"")
            .expect("oracle 欄位存在");
        let digits: String = value
            .trim_start_matches(|ch: char| !ch.is_ascii_digit())
            .chars()
            .take_while(char::is_ascii_digit)
            .collect();
        assert_eq!(digits.parse::<usize>().unwrap(), PARSE_DEPTH_LIMIT);
    }
}
