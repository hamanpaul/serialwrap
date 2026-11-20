//! `Mono`（奈秒）與秒的換算（P7 Task 0，08 §0.3）：不改 `Mono = u64` 型別，只提供兩個自由函式。

use crate::Mono;

/// 秒（`f64`）→ `Mono` 奈秒；負值與 NaN 為 0，超出 `u64` 飽和。
pub fn from_secs_f64(s: f64) -> Mono {
    if !(s > 0.0) {
        return 0;
    }
    let ns = s * 1e9;
    if ns >= u64::MAX as f64 {
        u64::MAX
    } else {
        ns.round() as u64
    }
}

/// `Mono` 奈秒 → 秒（`f64`）。
pub fn as_secs_f64(m: Mono) -> f64 {
    m as f64 / 1e9
}
