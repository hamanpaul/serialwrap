//! `skill` 指令（P3-T15）：`cli.py:_run_skill` 對照。只使用 `crate::assets`。
//!
//! 唯讀、不需 daemon。`--platform auto`（預設）依 `ClientCaps::default_skill` 選擇（Q-27，不看 `SERIALWRAP_RPC_BACKEND`）。
//! 輸出經 `TextIo::out_str`，Windows 上 `\n` 轉為 `\r\n`（Q-05），不額外加換行（Q-27）。
//! Python 以 `read_text`（文字模式，universal newlines）讀取資產；內嵌原檔若含 CRLF（例如 Windows autocrlf checkout），
//! 此處先以 [`universal_newlines`] 正規化，再由 `TextIo` 依平台轉換，避免輸出 `\r\r\n`。

use crate::argparse::Namespace;
use crate::assets::{SKILL_MD, SKILL_WINDOWS_MD};
use crate::ctx::CliCtx;
use sw_pal::SkillPlatform;
use sw_proto::pycompat::universal_newlines;

pub fn run_skill(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    let windows = match ns.str("platform").unwrap_or("auto") {
        "auto" => ctx.p.caps.default_skill == SkillPlatform::Windows,
        "windows" => true,
        _ => false,
    };
    let text = universal_newlines(if windows { SKILL_WINDOWS_MD } else { SKILL_MD });
    let _ = ctx.io.out_str(&text);
    0
}
