//! `profile test`（`cli.py:1804-1905`）：離線 regex 診斷（P3-T15）。
//!
//! 純讀檔＋`re.search`：不連 daemon、不碰 UART；正常路徑 exit code 恆為 0（Q-26），載入錯誤與未攔截例外為 1。
//! - `re.error` 回報於 `error` 欄位（Python 只攔截 `re.error`）。
//! - Python 未攔截的例外（`RecursionError`、`OverflowError`、`ValueError`，例如 `(?a)(?u)` 的旗標衝突）
//!   以 stderr 一行、exit 1、stdout 空結束。`(?L)` 等 str pattern 不允許的旗標屬 `re.error`，回報於 `error` 欄位。
//! - `--sample` 以 `errors="replace"` 解碼並套用 universal newlines；`matched_line` 為命中處所在的完整原始行。

use std::path::PathBuf;

use crate::argparse::Namespace;
use crate::argv;
use crate::commands::rpc_cmds::internal_error;
use crate::ctx::CliCtx;
use crate::output::print_json;
use sw_proto::config_types::{load_profiles, ProfileFs, ProfileTemplate};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pycompat::{
    char_index_to_byte, decode_utf8_replace, py_strip, splitlines, universal_newlines, PyOsError,
};
use sw_proto::pyfmt::py_repr_argv;
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::pypath::{expanduser as py_expanduser, HomeLookup, PathFlavor};
use sw_proto::pyre::{compile, CompileError, PyFlags, PyRegex};
use sw_proto::runtime_paths::EnvSnapshot;

/// 單一 pattern 的檢查（`_profile_test_check`）。
///
/// 空 pattern 不編譯；`error` 欄位只承接 `re.error`。本函式對所有輸入都回傳結果（全函式），
/// Python 未攔截例外於此以 `error` 欄位回報；CLI 路徑（[`run_profile_test`]）改以 exit 1 結束。
pub fn profile_test_check(pattern: &str, sample: &str) -> PyDict {
    search_pattern(pattern, sample)
        .unwrap_or_else(|message| check_result(pattern, None, Some(message)))
}

/// 取樣本最後一個非空白行（`_profile_test_last_nonblank_line`，`str.splitlines` 與 `str.strip` 語意）。
pub fn last_nonblank_line(text: &str) -> Option<&str> {
    splitlines(text)
        .into_iter()
        .rev()
        .find(|line| !py_strip(line).is_empty())
}

/// bootloader pattern 對「最後一個非空白行」的檢查（`_profile_test_bootloader_check`）。
///
/// 與 [`profile_test_check`] 相同，為全函式；未攔截例外改以 `error` 欄位回報。
pub fn bootloader_check(pattern: &str, last: Option<&str>) -> PyDict {
    bootloader_pattern(pattern, last)
        .unwrap_or_else(|message| check_result(pattern, None, Some(message)))
}

/// `cli.py:_run_profile_test`：`load_profiles` → 找 template → 讀 sample → 各 regex 檢查 → stdout JSON。
pub fn run_profile_test(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    let profile = ns.str("profile").unwrap_or("").to_string();
    let sample = ns.str("sample").unwrap_or("").to_string();
    let profile_dir = match ns.str("profile_dir") {
        Some(dir) if !dir.is_empty() => dir.to_string(),
        _ => ctx.paths.profile_dir.clone(),
    };

    let loaded = {
        let fs = CliProfileFs {
            p: ctx.p,
            env: &ctx.env,
        };
        load_profiles(&profile_dir, &fs)
    };
    let loaded = match loaded {
        Ok(loaded) => loaded,
        // ConfigError（YAML 語法、OSError、UTF-8）：Python 未攔截，traceback 結束；stderr 收斂為一行。
        Err(error) => return internal_error(ctx, &error.to_string().replace('\n', " ")),
    };

    let Some(template) = loaded
        .templates
        .iter()
        .find(|template| template.profile_name == profile)
    else {
        let mut names: Vec<&str> = loaded
            .templates
            .iter()
            .map(|template| template.profile_name.as_str())
            .collect();
        names.sort_unstable();
        names.dedup();
        return unknown_profile(ctx, &profile, &profile_dir, &names);
    };

    let bytes = match ctx.p.fs.read(&PathBuf::from(argv::decode(&sample))) {
        Ok(bytes) => bytes,
        Err(error) => {
            let message = format!("{}: {}", error.py_message, py_repr_argv(&sample));
            let resp = PyDict::from_pairs(vec![
                ("ok".to_string(), PyValue::Bool(false)),
                (
                    "error_code".to_string(),
                    PyValue::Str(ErrorCode::SampleReadFailed.as_str().to_string()),
                ),
                ("message".to_string(), PyValue::Str(message)),
            ]);
            let _ = print_json(&mut ctx.io, &resp);
            return 0;
        }
    };
    let text = universal_newlines(&decode_utf8_replace(&bytes));

    let checks = match run_checks(template, &text) {
        Ok(checks) => checks,
        Err(message) => return internal_error(ctx, &message),
    };
    // Q-19 (c)：`profile_dir`、`sample` 原文含非 UTF-8 時，Python 的 strict stdout 於 `_print` 失敗（exit 1、stdout 空）。
    if argv::has_escape(&profile_dir) || argv::has_escape(&sample) {
        return internal_error(ctx, argv::NOT_UTF8_MESSAGE);
    }

    let resp = PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(true)),
        (
            "profile".to_string(),
            PyValue::Str(template.profile_name.clone()),
        ),
        (
            "platform".to_string(),
            PyValue::Str(template.platform.clone()),
        ),
        ("profile_dir".to_string(), PyValue::Str(profile_dir)),
        ("sample".to_string(), PyValue::Str(sample)),
        ("checks".to_string(), PyValue::Dict(checks)),
    ]);
    let _ = print_json(&mut ctx.io, &resp);
    0
}

/// `UNKNOWN_PROFILE`（exit 0）：`available` 為 **最後一個** YAML 檔的 template 名稱排序集合（Q-26）。
fn unknown_profile(ctx: &mut CliCtx, profile: &str, profile_dir: &str, names: &[&str]) -> i32 {
    if argv::has_escape(profile) || argv::has_escape(profile_dir) {
        return internal_error(ctx, argv::NOT_UTF8_MESSAGE);
    }
    let available = names
        .iter()
        .map(|name| PyValue::Str((*name).to_string()))
        .collect();
    let resp = PyDict::from_pairs(vec![
        ("ok".to_string(), PyValue::Bool(false)),
        (
            "error_code".to_string(),
            PyValue::Str(ErrorCode::UnknownProfile.as_str().to_string()),
        ),
        ("profile".to_string(), PyValue::Str(profile.to_string())),
        (
            "profile_dir".to_string(),
            PyValue::Str(profile_dir.to_string()),
        ),
        ("available".to_string(), PyValue::List(available)),
    ]);
    let _ = print_json(&mut ctx.io, &resp);
    0
}

/// 四組 regex 檢查（`cli.py:1889-1896`）。
///
/// 求值順序與 Python dict 字面量相同（prompt → login → password → bootloader），使多個未攔截例外並存時
/// stderr 顯示的是 Python 先拋出的那一個。
fn run_checks(template: &ProfileTemplate, text: &str) -> Result<PyDict, String> {
    let prompt = search_pattern(&template.prompt_regex, text)?;
    let login = search_pattern(&template.login_regex, text)?;
    let password = search_pattern(&template.password_regex, text)?;
    let last = last_nonblank_line(text);
    let mut bootloader = Vec::with_capacity(template.bootloader_prompts.len());
    for pattern in &template.bootloader_prompts {
        bootloader.push(PyValue::Dict(bootloader_pattern(pattern, last)?));
    }
    Ok(PyDict::from_pairs(vec![
        ("prompt_regex".to_string(), PyValue::Dict(prompt)),
        ("login_regex".to_string(), PyValue::Dict(login)),
        ("password_regex".to_string(), PyValue::Dict(password)),
        ("bootloader_prompts".to_string(), PyValue::List(bootloader)),
    ]))
}

/// `Ok` 為 Python 會回報於 `error` 欄位的結果；`Err` 為 Python 未攔截例外的訊息。
fn search_pattern(pattern: &str, sample: &str) -> Result<PyDict, String> {
    if pattern.is_empty() {
        return Ok(check_result(pattern, None, None));
    }
    let regex = match compile_pattern(pattern) {
        Ok(regex) => regex,
        Err(CompileFailure::Reported(message)) => {
            return Ok(check_result(pattern, None, Some(message)))
        }
        Err(CompileFailure::Uncaught(message)) => return Err(message),
    };
    match regex.search(sample) {
        Some(found) => Ok(check_result(
            pattern,
            Some(line_containing(sample, found.start)),
            None,
        )),
        None => Ok(check_result(pattern, None, None)),
    }
}

/// `_profile_test_bootloader_check`：只檢查最後一個非空白行；`None`（樣本全為空白）時不命中。
fn bootloader_pattern(pattern: &str, last: Option<&str>) -> Result<PyDict, String> {
    if pattern.is_empty() {
        return Ok(check_result(pattern, None, None));
    }
    let regex = match compile_pattern(pattern) {
        Ok(regex) => regex,
        Err(CompileFailure::Reported(message)) => {
            return Ok(check_result(pattern, None, Some(message)))
        }
        Err(CompileFailure::Uncaught(message)) => return Err(message),
    };
    let matched = last.filter(|line| regex.search(line).is_some());
    Ok(check_result(pattern, matched, None))
}

/// 命中處所在的完整原始行（`cli.py:1820-1825`）。`start` 為 code point 索引。
fn line_containing(text: &str, start: usize) -> &str {
    let start = char_index_to_byte(text, start);
    let line_start = text[..start].rfind('\n').map_or(0, |index| index + 1);
    let line_end = text[start..]
        .find('\n')
        .map_or(text.len(), |index| start + index);
    &text[line_start..line_end]
}

/// 單一 regex 的 Python 語意失敗分類。
enum CompileFailure {
    /// Python `re.error`：寫入 `error` 欄位。
    Reported(String),
    /// Python 未攔截：RecursionError、OverflowError、ValueError。
    Uncaught(String),
}

/// `re.compile(pattern)`（旗標 0）的錯誤分類。
fn compile_pattern(pattern: &str) -> Result<PyRegex, CompileFailure> {
    compile(pattern, PyFlags::default()).map_err(|error| match error {
        CompileError::Re(err) if err.value_error => CompileFailure::Uncaught(err.to_string()),
        CompileError::Re(err) => CompileFailure::Reported(err.to_string()),
        CompileError::Recursion => {
            CompileFailure::Uncaught("maximum recursion depth exceeded".to_string())
        }
        CompileError::Overflow(message) => CompileFailure::Uncaught(message),
        // 語法有效但執行引擎不能忠實轉譯（已登記殘餘差異 EVT-D-S15）。
        CompileError::Untranslatable(construct) => {
            CompileFailure::Reported(format!("untranslatable: {construct}"))
        }
    })
}

/// 單一 pattern 的結果字典；`matched_line` 為 `Some` 即代表命中。
fn check_result(pattern: &str, matched_line: Option<&str>, error: Option<String>) -> PyDict {
    PyDict::from_pairs(vec![
        ("pattern".to_string(), PyValue::Str(pattern.to_string())),
        ("matched".to_string(), PyValue::Bool(matched_line.is_some())),
        (
            "matched_line".to_string(),
            matched_line.map_or(PyValue::Null, |line| PyValue::Str(line.to_string())),
        ),
        (
            "error".to_string(),
            error.map_or(PyValue::Null, PyValue::Str),
        ),
    ])
}

/// `ProfileFs` 的 PAL 轉接：檔案存取走 `FsRead`，`expanduser` 走 `SysEnv` 的家目錄查詢。
struct CliProfileFs<'a> {
    p: &'a sw_pal::ClientPlatform,
    env: &'a EnvSnapshot,
}

impl CliProfileFs<'_> {
    fn path_flavor(&self) -> PathFlavor {
        match self.p.caps.path_flavor {
            sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
            sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
        }
    }
}

impl ProfileFs for CliProfileFs<'_> {
    fn flavor(&self) -> PathFlavor {
        self.path_flavor()
    }

    fn is_dir(&self, p: &str) -> bool {
        self.p.fs.is_dir(&PathBuf::from(argv::decode(p)))
    }

    fn list_dir(&self, p: &str) -> Result<Vec<String>, PyOsError> {
        self.p
            .fs
            .list_dir(&PathBuf::from(argv::decode(p)))
            .map_err(|error| os_error(error, p))
    }

    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, PyOsError> {
        self.p
            .fs
            .read(&PathBuf::from(argv::decode(p)))
            .map_err(|error| os_error(error, p))
    }

    fn expanduser(&self, p: &str) -> String {
        py_expanduser(
            self.path_flavor(),
            p,
            self.env,
            &SysEnvHome(self.p.env.as_ref()),
        )
    }
}

struct SysEnvHome<'a>(&'a dyn sw_pal::SysEnv);

impl HomeLookup for SysEnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }

    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

/// PAL 錯誤轉 `OSError` 格式（`str(exc)` 含檔名 repr）。
fn os_error(error: sw_pal::PalError, filename: &str) -> PyOsError {
    PyOsError {
        errno: error.os_code.map(i64::from),
        winerror: error.winerror.map(i64::from),
        strerror: Some(error.strerror),
        filename: Some(filename.to_string()),
        plain: None,
    }
}
