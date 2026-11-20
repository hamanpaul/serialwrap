//! P6 golden TSV 讀取與 hex 工具（07 Task 2 建立，Task 3 共用）。
//!
//! 四欄 TSV：`<case_id>\t<op>\t<input>\t<expected>`。`input`／`expected` 保留 TSV 原字串、不做 hex 解碼
//! （各測試依 golden 格式自行 `unhex`／`unhex_utf8`）。golden 以 `include_str!` 嵌入，Windows runner 不需 staging 檔案。

#![allow(dead_code)]

/// golden 的一列。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub id: String,
    pub op: String,
    pub input: String,
    pub expected: String,
}

/// 解析整份 TSV（略過空行；欄數不是 4 即 panic）。
pub fn rows(tsv: &str) -> Vec<Row> {
    tsv.split('\n')
        .filter(|l| !l.is_empty())
        .map(|l| {
            let cols: Vec<&str> = l.split('\t').collect();
            assert_eq!(cols.len(), 4, "golden 列欄數必須為 4：{l:.80}");
            Row { id: cols[0].into(), op: cols[1].into(), input: cols[2].into(), expected: cols[3].into() }
        })
        .collect()
}

/// 小寫或大寫 hex → bytes（長度必須為偶數、只含 hex 字元，否則 panic）。
pub fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len() % 2 == 0, "hex 長度必須為偶數：{s:.40}");
    let nib = |c: u8| -> u8 {
        match c {
            b'0'..=b'9' => c - b'0',
            b'a'..=b'f' => c - b'a' + 10,
            b'A'..=b'F' => c - b'A' + 10,
            _ => panic!("非 hex 字元：{:?}", c as char),
        }
    };
    s.as_bytes().chunks(2).map(|p| (nib(p[0]) << 4) | nib(p[1])).collect()
}

/// hex → UTF-8 字串（golden 由 Python `str.encode("utf-8")` 產生，必為合法 UTF-8）。
pub fn unhex_utf8(s: &str) -> String {
    String::from_utf8(unhex(s)).expect("golden hex 應為合法 UTF-8")
}

/// xorshift64*（固定 seed 的可重現隨機序列）。
///
/// 全零狀態是 xorshift 的不動點（`SW_TEST_SEED=0` 會讓序列恆為 0），因此只能經 `new` 建構，
/// 並以 `seed | 1` 避開（parity-amendments-2026-10-26 #181，P6 審查 F8）；奇數 seed（含預設 0x5EED）序列不變。
pub struct XorShift(u64);

impl XorShift {
    pub fn new(seed: u64) -> Self {
        XorShift(seed | 1)
    }

    pub fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// `[0, n)`；`n == 0` 回 0。
    pub fn below(&mut self, n: u64) -> u64 {
        if n == 0 {
            0
        } else {
            self.next() % n
        }
    }
}
