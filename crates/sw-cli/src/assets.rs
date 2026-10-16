//! 內嵌資產（唯一定義處）：`include_str!` 路徑以 `crates/sw-cli/src/` 為基準（P3-T15）。

pub const SKILL_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL.md");
pub const SKILL_WINDOWS_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL_WINDOWS.md");
