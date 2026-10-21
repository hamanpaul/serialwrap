//! 測試通用輔助函式（sw-pal 測試共用）。
#![allow(dead_code)]

use std::ffi::OsString;
use std::path::PathBuf;
use std::time::Duration;

use sw_pal::traits::{EndpointSpec, SockTimeout};

/// 取得 repo 根目錄路徑。
///
/// 優先讀取執行期環境變數 `SW_TEST_DATA_DIR`；若未設定則以編譯期 `CARGO_MANIFEST_DIR/../..` 推導。
pub fn data_root() -> PathBuf {
    if let Ok(dir) = std::env::var("SW_TEST_DATA_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// OSError 基準檔（依平台；由 `scripts/rust/goldens/gen_oserror.py` 產生）。
pub fn oserror_file() -> &'static str {
    if cfg!(unix) {
        "oserror.linux.tsv"
    } else {
        "oserror.windows.tsv"
    }
}

/// errno 名稱基準檔（依平台；由 `scripts/rust/goldens/gen_oserror.py` 產生）。
pub fn errno_names_file() -> &'static str {
    if cfg!(unix) {
        "errno_names.linux.tsv"
    } else {
        "errno_names.windows.tsv"
    }
}

/// golden TSV 的一列（格式見 `scripts/rust/goldens/_tsv.py`）。
/// 欄位為 hex 編碼的 UTF-8，解碼後以 `\x1f` 拆成子欄位；解碼不依賴受測程式。
pub struct GoldenRow {
    pub id: String,
    pub op: String,
    pub input: Vec<String>,
    pub expected: Vec<String>,
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("golden 欄位必須為 hex"))
        .collect()
}

fn subfields(hex: &str) -> Vec<String> {
    let text = String::from_utf8(unhex(hex)).expect("golden 欄位必須為 UTF-8");
    text.split('\x1f').map(str::to_string).collect()
}

/// 讀取 `contracts/golden/<file>` 的全部列；檔案缺失即 panic（不得以 SKIP 代替）。
pub fn golden_rows(file: &str) -> Vec<GoldenRow> {
    let p = data_root().join("contracts/golden").join(file);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    text.lines()
        .filter(|l| !l.is_empty())
        .map(|l| {
            let f: Vec<&str> = l.split('\t').collect();
            assert_eq!(f.len(), 4, "golden 列必須為 4 欄：{l:?}");
            GoldenRow {
                id: f[0].into(),
                op: f[1].into(),
                input: subfields(f[2]),
                expected: subfields(f[3]),
            }
        })
        .collect()
}

fn opt_i32(s: &str) -> Option<i32> {
    if s.is_empty() {
        None
    } else {
        Some(s.parse().expect("golden errno 必須為整數"))
    }
}

/// `oserror.<os>.tsv` 的一列：操作輸入與 Python 觀察到的例外語意。
/// `expected` 子欄位順序：`exc_class`、`errno`、`winerror`、`strerror`、`message`、`errno_name`。
pub struct OsErrCase {
    pub id: String,
    pub op: String,
    pub input: Vec<String>,
    pub exc: String,
    pub errno: Option<i32>,
    pub winerror: Option<i32>,
    pub strerror: String,
    pub message: String,
    pub errno_name: String,
}

/// 讀取 OSError 基準檔的全部列。
pub fn oserror_cases(file: &str) -> Vec<OsErrCase> {
    golden_rows(file)
        .into_iter()
        .map(|r| {
            assert_eq!(r.expected.len(), 6, "{} expected 子欄位數必須為 6", r.id);
            let e = &r.expected;
            OsErrCase {
                exc: e[0].clone(),
                errno: opt_i32(&e[1]),
                winerror: opt_i32(&e[2]),
                strerror: e[3].clone(),
                message: e[4].clone(),
                errno_name: e[5].clone(),
                id: r.id,
                op: r.op,
                input: r.input,
            }
        })
        .collect()
}

impl OsErrCase {
    /// connect 類 case 的端點：`lit:` 路徑或 TCP 位址。`tmp:` 路徑需由測試自行建立 fixture，不在此解析。
    pub fn endpoint(&self) -> EndpointSpec {
        match self.op.as_str() {
            "connect_tcp" => EndpointSpec::Tcp {
                host: self.input[0].clone(),
                port: self.input[1].parse().expect("golden port 必須為整數"),
            },
            "connect_unix" => {
                let rest = self.input[0]
                    .strip_prefix("lit:")
                    .unwrap_or_else(|| panic!("{} 為 tmp: 路徑，需由測試建立 fixture", self.id));
                EndpointSpec::Unix(OsString::from(rest))
            }
            other => panic!("{}: op {other} 沒有端點", self.id),
        }
    }

    /// 最後一個輸入子欄位為 timeout：`0` 為非阻塞（Python `settimeout(0)`），其餘為秒數。
    pub fn timeout(&self) -> SockTimeout {
        let t = self.input.last().expect("golden 必須含 timeout 欄位");
        if t == "0" {
            SockTimeout::NonBlocking
        } else {
            SockTimeout::Blocking(Duration::from_secs_f64(
                t.parse().expect("golden timeout 必須為數字"),
            ))
        }
    }
}
