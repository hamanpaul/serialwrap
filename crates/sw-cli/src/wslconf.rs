//! WSL `/etc/wsl.conf` 檢查（DF-11；P4-T10 承接）。

pub fn merge_wsl_conf_systemd(existing: &str) -> String {
    let mut defaults = Vec::<(String, String)>::new();
    let mut sections = Vec::<(String, Vec<(String, String)>)>::new();
    let mut current: Option<usize> = None;
    let mut in_default = false;
    let mut last_option: Option<(bool, usize, usize)> = None;
    let mut pending_blank_lines = 0usize;
    let mut failed = false;

    for raw_line in existing.lines() {
        let line = raw_line.trim_end_matches('\r');
        let trimmed = line.trim();
        if trimmed.is_empty() {
            if last_option.is_some() {
                pending_blank_lines += 1;
            }
            continue;
        }
        if trimmed.starts_with('#') || trimmed.starts_with(';') {
            continue;
        }
        if trimmed.starts_with('[') {
            let Some(close) = trimmed[1..].find(']').map(|position| position + 1) else {
                failed = true;
                break;
            };
            let name = trimmed[1..close].to_string();
            if name.is_empty() {
                failed = true;
                break;
            }
            pending_blank_lines = 0;
            if name == "DEFAULT" {
                current = None;
                in_default = true;
            } else {
                if sections.iter().any(|(existing, _)| existing == &name) {
                    failed = true;
                    break;
                }
                sections.push((name, Vec::new()));
                current = Some(sections.len() - 1);
                in_default = false;
            }
            let _ = last_option.take();
            continue;
        }

        let indent = line.chars().take_while(|c| c.is_whitespace()).count();
        if indent > 0 {
            if let Some((is_default, option_index, option_indent)) = last_option {
                if indent <= option_indent {
                    let _ = last_option.take();
                } else {
                    let rows = if is_default {
                        &mut defaults
                    } else if let Some(index) = current {
                        &mut sections[index].1
                    } else {
                        failed = true;
                        break;
                    };
                    if let Some((_, value)) = rows.get_mut(option_index) {
                        for _ in 0..pending_blank_lines {
                            value.push('\n');
                        }
                        value.push('\n');
                        value.push_str(trimmed);
                        pending_blank_lines = 0;
                        continue;
                    }
                }
            }
        }

        pending_blank_lines = 0;

        let Some(section_index) = current else {
            if !in_default {
                failed = true;
                break;
            }
            // DEFAULT 項目可出現在一般區段之前或之後。
            let Some((key, value)) = split_option(line) else {
                failed = true;
                break;
            };
            if defaults.iter().any(|(old, _)| old == &key) {
                failed = true;
                break;
            }
            defaults.push((key, value));
            last_option = Some((true, defaults.len() - 1, indent));
            continue;
        };
        let Some((key, value)) = split_option(line) else {
            failed = true;
            break;
        };
        if sections[section_index].1.iter().any(|(old, _)| old == &key) {
            failed = true;
            break;
        }
        sections[section_index].1.push((key, value));
        last_option = Some((false, sections[section_index].1.len() - 1, indent));
    }

    if failed {
        defaults.clear();
        sections.clear();
    }

    if !sections.iter().any(|(name, _)| name == "boot") {
        sections.push(("boot".to_string(), Vec::new()));
    }
    let boot = sections
        .iter_mut()
        .find(|(name, _)| name == "boot")
        .unwrap();
    if let Some((_, value)) = boot.1.iter_mut().find(|(key, _)| key == "systemd") {
        *value = "true".to_string();
    } else {
        boot.1.push(("systemd".to_string(), "true".to_string()));
    }

    // ConfigParser 保留段落與選項插入順序。
    let mut output = String::new();
    if !defaults.is_empty() {
        output.push_str("[DEFAULT]\n");
        for (key, value) in defaults {
            write_option(&mut output, &key, &value);
        }
        output.push('\n');
    }
    for (name, rows) in sections {
        output.push('[');
        output.push_str(&name);
        output.push_str("]\n");
        for (key, value) in rows {
            write_option(&mut output, &key, &value);
        }
        output.push('\n');
    }
    output
}

fn split_option(line: &str) -> Option<(String, String)> {
    let equal = line.find('=');
    let colon = line.find(':');
    let split = match (equal, colon) {
        (Some(a), Some(b)) => a.min(b),
        (Some(a), None) | (None, Some(a)) => a,
        (None, None) => return None,
    };
    let key = line[..split].trim();
    if key.is_empty() {
        return None;
    }
    Some((key.to_string(), line[split + 1..].trim().to_string()))
}

fn write_option(output: &mut String, key: &str, value: &str) {
    output.push_str(key);
    output.push_str(" = ");
    output.push_str(&value.replace('\n', "\n\t"));
    output.push('\n');
}
