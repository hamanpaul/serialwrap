//! com0com 虛擬序列埠 pair 管理（SC-36，OD-15）。

use crate::device_source::windows_model::RegistryView;
use crate::traits::{PalError, PalErrorKind, PalResult};
use std::path::Path;

/// Windows 安裝 com0com 選用元件時建立的預設 pair（DUT 端 COM20、console 端 COM21；SC-36）。
pub const DEFAULT_PAIR: (&str, &str) = ("COM20", "COM21");

const COM0COM_PARAMETERS_KEY: &str =
    "HKLM\\SYSTEM\\CurrentControlSet\\Services\\com0com\\Parameters";

/// com0com `setupc list` 所列的一組配對序列埠。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PairInfo {
    pub index: u32,
    pub a_name: String,
    pub b_name: String,
}

/// 從 com0com 唯讀登錄參數列出完整 pair，供未提權的 CLI 查詢安裝狀態。
pub fn list_pairs_from_registry(registry: &dyn RegistryView) -> PalResult<Vec<PairInfo>> {
    use std::collections::BTreeMap;

    let mut endpoints = BTreeMap::<u32, (Option<String>, Option<String>)>::new();
    for endpoint in registry.enum_subkeys(COM0COM_PARAMETERS_KEY)? {
        let (side, digits) = if let Some(digits) = endpoint.strip_prefix("CNCA") {
            (0, digits)
        } else if let Some(digits) = endpoint.strip_prefix("CNCB") {
            (1, digits)
        } else {
            continue;
        };
        let Ok(index) = digits.parse::<u32>() else {
            continue;
        };
        let key = format!("{COM0COM_PARAMETERS_KEY}\\{endpoint}");
        let Some(port) = registry.query_string(&key, "PortName")? else {
            continue;
        };
        if validate_port(&port).is_err() {
            continue;
        }
        let pair = endpoints.entry(index).or_default();
        if side == 0 {
            pair.0 = Some(port);
        } else {
            pair.1 = Some(port);
        }
    }

    Ok(endpoints
        .into_iter()
        .filter_map(|(index, (a_name, b_name))| {
            Some(PairInfo {
                index,
                a_name: a_name?,
                b_name: b_name?,
            })
        })
        .collect())
}

/// 以索引配對 `setupc list` 的 CNCA/CNCB 輸出行。
pub fn parse_pairs(output: &str) -> Vec<PairInfo> {
    use std::collections::BTreeMap;

    let mut endpoints = BTreeMap::<u32, (Option<String>, Option<String>)>::new();
    for line in output.lines() {
        let line = line.trim();
        let Some((name, properties)) = line.split_once(char::is_whitespace) else {
            continue;
        };
        let (side, digits) = if let Some(digits) = name.strip_prefix("CNCA") {
            (0, digits)
        } else if let Some(digits) = name.strip_prefix("CNCB") {
            (1, digits)
        } else {
            continue;
        };
        let Ok(index) = digits.parse::<u32>() else {
            continue;
        };
        let Some(port) = properties
            .split_whitespace()
            .find_map(|field| field.strip_prefix("PortName="))
        else {
            continue;
        };
        let port = port.split(',').next().unwrap_or(port);
        if validate_port(port).is_err() {
            continue;
        }
        let pair = endpoints.entry(index).or_default();
        if side == 0 {
            pair.0 = Some(port.to_string());
        } else {
            pair.1 = Some(port.to_string());
        }
    }

    endpoints
        .into_iter()
        .filter_map(|(index, (a_name, b_name))| {
            Some(PairInfo {
                index,
                a_name: a_name?,
                b_name: b_name?,
            })
        })
        .collect()
}

/// 建立一組 com0com pair。
pub fn install_pair(setupc: &Path, a: &str, b: &str) -> PalResult<PairInfo> {
    validate_port(a)?;
    validate_port(b)?;
    if a.eq_ignore_ascii_case(b) {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "pair 兩端不得使用相同 COM 埠",
        ));
    }
    run_setupc(
        setupc,
        &[
            "install".into(),
            format!("PortName={a}"),
            format!("PortName={b}"),
        ],
    )?;
    list_pairs(setupc)?
        .into_iter()
        .find(|pair| pair.a_name.eq_ignore_ascii_case(a) && pair.b_name.eq_ignore_ascii_case(b))
        .ok_or_else(|| {
            PalError::plain(
                PalErrorKind::Io,
                "setupc 已成功但找不到新建立的 com0com pair",
            )
        })
}

/// 依 pair 索引移除 com0com pair。
pub fn remove_pair(setupc: &Path, pair_index: u32) -> PalResult<()> {
    run_setupc(setupc, &["remove".into(), pair_index.to_string()]).map(|_| ())
}

/// 列出 com0com pair；只執行唯讀的 `setupc list`。
pub fn list_pairs(setupc: &Path) -> PalResult<Vec<PairInfo>> {
    let output = run_setupc(setupc, &["list".into()])?;
    Ok(parse_pairs(&output))
}

fn validate_port(port: &str) -> PalResult<()> {
    let digits = port
        .strip_prefix("COM")
        .or_else(|| port.strip_prefix("com"))
        .ok_or_else(|| PalError::plain(PalErrorKind::InvalidInput, "COM 埠名稱格式無效"))?;
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(PalError::plain(
            PalErrorKind::InvalidInput,
            "COM 埠名稱格式無效",
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn setupc_command(setupc: &Path, args: &[String]) -> std::process::Command {
    let mut command = std::process::Command::new(setupc);
    command.current_dir(setupc.parent().unwrap_or_else(|| Path::new(".")));
    command.arg("--silent");
    if matches!(args.first().map(String::as_str), Some("install" | "remove")) {
        command.args(["--wait", "30"]);
    }
    command.args(args);
    command
}

#[cfg(windows)]
fn run_setupc(setupc: &Path, args: &[String]) -> PalResult<String> {
    let output = setupc_command(setupc, args)
        .output()
        .map_err(|error| PalError::plain(PalErrorKind::Io, format!("無法執行 setupc：{error}")))?;
    if !output.status.success() {
        return Err(PalError::plain(
            PalErrorKind::Io,
            format!(
                "setupc 結束碼 {}：{}",
                output.status.code().unwrap_or(-1),
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

#[cfg(unix)]
fn run_setupc(_setupc: &Path, _args: &[String]) -> PalResult<String> {
    Err(PalError::plain(
        PalErrorKind::Unsupported,
        "com0com 僅支援 Windows",
    ))
}

#[cfg(test)]
mod tests {
    use super::{parse_pairs, setupc_command, validate_port, COM0COM_PARAMETERS_KEY};
    use crate::device_source::windows_model::RegistryView;
    use crate::traits::{PalError, PalErrorKind, PalResult};

    struct Registry(Vec<(String, String)>);

    impl RegistryView for Registry {
        fn enum_values(&self, _key: &str) -> PalResult<Vec<(String, String)>> {
            Ok(Vec::new())
        }

        fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>> {
            if key == COM0COM_PARAMETERS_KEY {
                Ok(self.0.iter().map(|(name, _)| name.clone()).collect())
            } else {
                Ok(Vec::new())
            }
        }

        fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>> {
            if name != "PortName" {
                return Ok(None);
            }
            let prefix = format!("{COM0COM_PARAMETERS_KEY}\\");
            let Some(endpoint) = key.strip_prefix(&prefix) else {
                return Ok(None);
            };
            Ok(self
                .0
                .iter()
                .find(|(name, _)| name == endpoint)
                .map(|(_, port)| port.clone()))
        }
    }

    struct FailingRegistry;

    impl RegistryView for FailingRegistry {
        fn enum_values(&self, _key: &str) -> PalResult<Vec<(String, String)>> {
            Err(PalError::plain(PalErrorKind::Io, "registry read failed"))
        }

        fn enum_subkeys(&self, _key: &str) -> PalResult<Vec<String>> {
            Err(PalError::plain(PalErrorKind::Io, "registry read failed"))
        }

        fn query_string(&self, _key: &str, _name: &str) -> PalResult<Option<String>> {
            Err(PalError::plain(PalErrorKind::Io, "registry read failed"))
        }
    }

    #[test]
    fn parses_complete_com0com_pairs() {
        let pairs = parse_pairs(
            "CNCA0 PortName=COM20,EmuBR=yes\nCNCB0 PortName=COM21,EmuBR=yes\nCNCA1 PortName=COM30\n",
        );
        assert_eq!(pairs.len(), 1);
        assert_eq!(pairs[0].index, 0);
        assert_eq!(pairs[0].a_name, "COM20");
        assert_eq!(pairs[0].b_name, "COM21");
    }

    #[test]
    fn ignores_malformed_or_incomplete_setupc_rows() {
        let pairs = parse_pairs(
            "CNCA0 PortName=COM20,EmuBR=yes\nCNCB0 PortName=not-a-port\nCNCA1 PortName=COM31\nCNCB2 PortName=COM42,EmuBR=yes\nCNCAx PortName=COM40,EmuBR=yes\n",
        );
        assert!(pairs.is_empty(), "不完整或無效 pair 不得被視為已安裝：{pairs:?}");
    }

    #[test]
    fn validates_com_port_names() {
        assert!(validate_port("COM20").is_ok());
        assert!(validate_port("COM20; whoami").is_err());
    }

    #[test]
    fn lists_complete_pairs_from_registry_without_setupc() {
        let registry = Registry(vec![
            ("CNCA0".into(), "COM20".into()),
            ("CNCB0".into(), "COM21".into()),
            ("CNCA1".into(), "COM30".into()),
            ("CNCA2".into(), "--".into()),
            ("CNCB2".into(), "COM32".into()),
        ]);
        let pairs = super::list_pairs_from_registry(&registry).expect("登錄列舉應成功");
        assert_eq!(pairs.len(), 1);
        assert_eq!(pairs[0].index, 0);
        assert_eq!(pairs[0].a_name, "COM20");
        assert_eq!(pairs[0].b_name, "COM21");
    }

    #[test]
    fn registry_errors_are_returned_for_caller_to_handle() {
        let error = super::list_pairs_from_registry(&FailingRegistry)
            .expect_err("登錄讀取錯誤不得偽裝成空 pair 清單");
        assert_eq!(error.kind, PalErrorKind::Io);
    }

    #[test]
    fn setupc_command_uses_payload_directory_and_noninteractive_wait_args() {
        let setupc = std::path::Path::new("C:/bundle/optional/com0com/setupc.exe");
        let args = vec!["install".into(), "PortName=COM20".into(), "PortName=COM21".into()];
        let command = setupc_command(setupc, &args);
        assert_eq!(
            command.get_current_dir(),
            Some(std::path::Path::new("C:/bundle/optional/com0com"))
        );
        assert_eq!(
            command
                .get_args()
                .map(|arg| arg.to_string_lossy().into_owned())
                .collect::<Vec<_>>(),
            [
                "--silent",
                "--wait",
                "30",
                "install",
                "PortName=COM20",
                "PortName=COM21"
            ]
        );
    }
}
