//! 以使用者明確同意的 UAC 權限執行 com0com pair 管理。

use std::path::PathBuf;

fn usage() -> ! {
    eprintln!("用法：sw-com0com-helper install|remove --pair CNCA0=COM20,CNCB0=COM21");
    std::process::exit(2);
}

fn parse_pair(value: &str) -> Result<(String, String), String> {
    let (a, b) = value
        .split_once(',')
        .ok_or_else(|| "pair 格式無效".to_string())?;
    let (a_name, a_port) = a
        .split_once('=')
        .ok_or_else(|| "pair 格式無效".to_string())?;
    let (b_name, b_port) = b
        .split_once('=')
        .ok_or_else(|| "pair 格式無效".to_string())?;
    let a_index = a_name
        .strip_prefix("CNCA")
        .ok_or_else(|| "pair 端點必須為 CNCA<n>".to_string())?;
    let b_index = b_name
        .strip_prefix("CNCB")
        .ok_or_else(|| "pair 端點必須為 CNCB<n>".to_string())?;
    if a_index.is_empty()
        || a_index != b_index
        || !a_index.bytes().all(|byte| byte.is_ascii_digit())
        || a_port.is_empty()
        || b_port.is_empty()
        || !a_port.starts_with("COM")
        || !b_port.starts_with("COM")
        || a_port.eq_ignore_ascii_case(b_port)
    {
        return Err("pair 格式或 COM 埠名稱無效".into());
    }
    Ok((a_port.to_string(), b_port.to_string()))
}

fn setupc_path() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|error| format!("無法定位 helper：{error}"))?;
    let version_dir = exe
        .parent()
        .and_then(std::path::Path::parent)
        .ok_or_else(|| "helper 不在 release bundle 的 bin 目錄".to_string())?;
    Ok(version_dir
        .join("optional")
        .join("com0com")
        .join("setupc.exe"))
}

fn run() -> Result<(), String> {
    let mut args = std::env::args().skip(1);
    let action = args.next().unwrap_or_else(|| usage());
    if action != "install" && action != "remove" {
        usage();
    }
    if args.next().as_deref() != Some("--pair") {
        usage();
    }
    let pair = parse_pair(&args.next().unwrap_or_else(|| usage()))?;
    if args.next().is_some() {
        usage();
    }
    let setupc = setupc_path()?;

    if action == "install" {
        let existing = sw_pal::com0com::list_pairs(&setupc).map_err(|error| error.py_message)?;
        let installed = if let Some(existing) = existing.into_iter().find(|item| {
            item.a_name.eq_ignore_ascii_case(&pair.0) && item.b_name.eq_ignore_ascii_case(&pair.1)
        }) {
            existing
        } else {
            match sw_pal::com0com::install_pair(&setupc, &pair.0, &pair.1) {
                Ok(installed) => installed,
                Err(install_error) => {
                    let after = sw_pal::com0com::list_pairs(&setupc)
                        .map_err(|list_error| list_error.py_message)?;
                    after
                        .into_iter()
                        .find(|item| {
                            item.a_name.eq_ignore_ascii_case(&pair.0)
                                && item.b_name.eq_ignore_ascii_case(&pair.1)
                        })
                        .ok_or(install_error.py_message)?
                }
            }
        };
        println!(
            "com0com pair 已就緒 {} / {}",
            installed.a_name, installed.b_name
        );
    } else {
        let installed = sw_pal::com0com::list_pairs(&setupc).map_err(|error| error.py_message)?;
        let Some(pair_info) = installed.into_iter().find(|item| {
            item.a_name.eq_ignore_ascii_case(&pair.0) && item.b_name.eq_ignore_ascii_case(&pair.1)
        }) else {
            println!("com0com pair 不存在，無需移除");
            return Ok(());
        };
        sw_pal::com0com::remove_pair(&setupc, pair_info.index).map_err(|error| error.py_message)?;
        println!("已移除 com0com pair {} / {}", pair.0, pair.1);
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("sw-com0com-helper：{error}");
        std::process::exit(1);
    }
}
