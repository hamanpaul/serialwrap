//! 內嵌資產（唯一定義處）：`include_str!` 路徑以 `crates/sw-cli/src/` 為基準（P3-T15）。

pub const SKILL_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL.md");
pub const SKILL_WINDOWS_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL_WINDOWS.md");

pub struct Asset {
    pub rel: &'static str,
    pub bytes: &'static [u8],
}

pub const PROFILE_ASSETS: &[Asset] = &[];
pub const SKILL_ASSETS: &[Asset] = &[];
pub const TOOL_ASSETS: &[Asset] = &[];

pub const WRAPPER_MAP: &[(&str, &str)] = &[
    ("minicom_router.sh", "serialwrap-minicom"),
    ("minicom-broker.sh", "serialwrap-minicom-broker"),
    ("minicom-raw.sh", "serialwrap-minicom-raw"),
];
