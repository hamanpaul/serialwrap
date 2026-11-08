//! 內嵌資產（唯一定義處）：`include_str!` 與 `include_bytes!` 路徑以 `crates/sw-cli/src/` 為基準（P3-T15）。

pub const SKILL_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL.md");
pub const SKILL_WINDOWS_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL_WINDOWS.md");

pub struct Asset {
    pub rel: &'static str,
    pub bytes: &'static [u8],
}

pub const PROFILE_ASSETS: &[Asset] = &[
    Asset {
        rel: "profiles/default.yaml",
        bytes: include_bytes!("../../../sw_core/assets/profiles/default.yaml"),
    },
    Asset {
        rel: "profiles/brcm.env.example",
        bytes: include_bytes!("../../../sw_core/assets/profiles/brcm.env.example"),
    },
];

pub const SKILL_ASSETS: &[Asset] = &[
    Asset {
        rel: "skill/SKILL.md",
        bytes: include_bytes!("../../../sw_core/assets/skill/SKILL.md"),
    },
    Asset {
        rel: "skill/SKILL_WINDOWS.md",
        bytes: include_bytes!("../../../sw_core/assets/skill/SKILL_WINDOWS.md"),
    },
];

pub const TOOL_ASSETS: &[Asset] = &[
    Asset {
        rel: "tools/minicom_router.sh",
        bytes: include_bytes!("../../../sw_core/assets/tools/minicom_router.sh"),
    },
    Asset {
        rel: "tools/minicom-broker.sh",
        bytes: include_bytes!("../../../sw_core/assets/tools/minicom-broker.sh"),
    },
    Asset {
        rel: "tools/minicom-raw.sh",
        bytes: include_bytes!("../../../sw_core/assets/tools/minicom-raw.sh"),
    },
];

pub const WRAPPER_MAP: &[(&str, &str)] = &[
    ("minicom_router.sh", "serialwrap-minicom"),
    ("minicom-broker.sh", "serialwrap-minicom-broker"),
    ("minicom-raw.sh", "serialwrap-minicom-raw"),
];
