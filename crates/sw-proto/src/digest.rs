//! SHA-256 摘要工具，供跨 crate 的 bundle 與 manifest 程式共用。

use sha2::{Digest, Sha256};

/// 計算位元組內容的 64 字元小寫十六進位 SHA-256。
pub fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    hex_bytes(&digest)
}

/// 增量計算 SHA-256，避免大型 bundle 檔案必須先載入記憶體。
pub struct Sha256Stream(Sha256);

impl Sha256Stream {
    /// 建立空摘要。
    pub fn new() -> Self {
        Self(Sha256::new())
    }

    /// 加入下一段資料。
    pub fn update(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    /// 完成摘要並回傳小寫十六進位字串。
    pub fn hex(self) -> String {
        hex_bytes(&self.0.finalize())
    }
}

impl Default for Sha256Stream {
    fn default() -> Self {
        Self::new()
    }
}

fn hex_bytes(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(HEX[(byte >> 4) as usize] as char);
        result.push(HEX[(byte & 0x0f) as usize] as char);
    }
    result
}
