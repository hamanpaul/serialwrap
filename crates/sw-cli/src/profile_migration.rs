//! p290：只對管理目錄的 prpl template 精確補入逾時救援鍵。

use sw_proto::pycompat::py_strip;
use sw_proto::yaml11::{py_str_of, py_truthy_of, safe_load, YMap, YValue};

#[derive(Clone, Debug)]
enum SourceTemplate {
    Root,
    Mapping(String),
    Sequence(usize),
}

#[derive(Clone, Debug)]
struct Candidate {
    source: SourceTemplate,
    label: String,
}

#[derive(Clone, Debug)]
struct Line {
    start: usize,
    end: usize,
    content: String,
    indent: usize,
}

type MigratedProfile = (Vec<u8>, Vec<String>);

/// 回傳原始位元組與新欄位所在 template；`None` 表示無需遷移。
pub(crate) fn migrate_bytes(bytes: &[u8]) -> Result<Option<MigratedProfile>, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "profile 不是 UTF-8".to_string())?;
    let parsed_text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let root = match safe_load(parsed_text) {
        Ok(root) => root,
        Err(error) if !looks_like_profile_document(parsed_text) => return Ok(None),
        Err(error) => return Err(format!("YAML 無法解析：{error}")),
    };
    let Some(root_map) = as_map(&root) else {
        return Ok(None);
    };
    let candidates = migration_candidates(root_map);
    if candidates.is_empty() {
        return Ok(None);
    }

    let lines = lines(text)?;
    let mut insertions = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let (position, indent) = locate_insertion(text, &lines, &candidate.source)?;
        insertions.push((position, indent, candidate.label));
    }
    insertions.sort_by_key(|(position, _, _)| std::cmp::Reverse(*position));

    let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
    let mut changed = text.to_string();
    let mut labels = Vec::with_capacity(insertions.len());
    for (position, indent, label) in insertions {
        let needs_prefix = position > 0 && changed.as_bytes().get(position - 1) != Some(&b'\n');
        let needs_suffix = position < changed.len() || changed.ends_with('\n');
        let mut insertion = String::new();
        if needs_prefix {
            insertion.push_str(newline);
        }
        insertion.push_str(&" ".repeat(indent));
        insertion.push_str("timeout_recovery_keys: [CTRL_C]");
        if needs_suffix {
            insertion.push_str(newline);
        }
        changed.insert_str(position, &insertion);
        labels.push(label);
    }
    Ok(Some((changed.into_bytes(), labels)))
}

/// `--force` 與 system staging 在取代 asset 前使用；只要 YAML 任一層已含該鍵便保留原檔。
pub(crate) fn contains_timeout_key(bytes: &[u8]) -> Result<bool, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "profile 不是 UTF-8".to_string())?;
    let parsed_text = text.strip_prefix('\u{feff}').unwrap_or(text);
    match safe_load(parsed_text) {
        Ok(root) => Ok(contains_key_recursive(&root, "timeout_recovery_keys")),
        Err(error) if !looks_like_profile_document(parsed_text) => Ok(false),
        Err(error) => Err(format!("YAML 無法解析：{error}")),
    }
}

fn as_map(value: &YValue) -> Option<&YMap> {
    if let YValue::Map(map) = value {
        Some(map)
    } else {
        None
    }
}

fn migration_candidates(root: &YMap) -> Vec<Candidate> {
    let mut candidates = Vec::new();
    let mut recognized_templates = 0usize;
    match root.get_str("profiles") {
        Some(YValue::Map(profiles)) => {
            for (key, value) in &profiles.0 {
                let Some(template) = as_map(value) else {
                    continue;
                };
                let name = py_strip(&py_str_of(key)).to_string();
                if name.is_empty() {
                    continue;
                }
                recognized_templates += 1;
                if is_prpl_missing_keys(template) {
                    candidates.push(Candidate {
                        source: SourceTemplate::Mapping(name.clone()),
                        label: name,
                    });
                }
            }
        }
        Some(YValue::Seq(rows)) => {
            for (index, row) in rows.iter().enumerate() {
                let Some(template) = as_map(row) else {
                    continue;
                };
                let name = template
                    .get_str("name")
                    .filter(|value| py_truthy_of(value))
                    .or_else(|| {
                        template
                            .get_str("profile_name")
                            .filter(|value| py_truthy_of(value))
                    })
                    .map(|value| py_strip(&py_str_of(value)).to_string())
                    .unwrap_or_default();
                if name.is_empty() {
                    continue;
                }
                recognized_templates += 1;
                if is_prpl_missing_keys(template) {
                    candidates.push(Candidate {
                        source: SourceTemplate::Sequence(index),
                        label: name,
                    });
                }
            }
        }
        _ => {}
    }

    // 與 `load_templates()` 一致：profiles 無有效 template 時以頂層 mapping 作 template。
    if recognized_templates == 0 && is_prpl_missing_keys(root) {
        candidates.push(Candidate {
            source: SourceTemplate::Root,
            label: "(top-level)".to_string(),
        });
    }
    candidates
}

fn is_prpl_missing_keys(template: &YMap) -> bool {
    if template.contains_str("timeout_recovery_keys") {
        return false;
    }
    let platform = template
        .get_str("platform")
        .filter(|value| py_truthy_of(value))
        .map(|value| py_strip(&py_str_of(value)).to_lowercase())
        .unwrap_or_default();
    platform == "prpl"
}

fn contains_key_recursive(value: &YValue, key: &str) -> bool {
    match value {
        YValue::Map(map) => map.0.iter().any(|(name, value)| {
            matches!(name, YValue::Str(s) if s == key) || contains_key_recursive(value, key)
        }),
        YValue::Seq(items) | YValue::Set(items) => {
            items.iter().any(|item| contains_key_recursive(item, key))
        }
        _ => false,
    }
}

fn looks_like_profile_document(text: &str) -> bool {
    text.lines().any(|line| {
        let line = line.trim_start_matches('\u{feff}').trim_start();
        line.starts_with("profiles:")
            || line.starts_with("platform:")
            || line.starts_with("profile_name:")
    })
}

fn lines(text: &str) -> Result<Vec<Line>, String> {
    let mut result = Vec::new();
    let mut start = 0usize;
    while start < text.len() {
        let end = text[start..]
            .find('\n')
            .map(|offset| start + offset + 1)
            .unwrap_or(text.len());
        let mut content = text[start..end]
            .trim_end_matches('\n')
            .trim_end_matches('\r')
            .to_string();
        if start == 0 {
            content = content.trim_start_matches('\u{feff}').to_string();
        }
        let indent = content.bytes().take_while(|byte| *byte == b' ').count();
        if content.as_bytes().get(indent) == Some(&b'\t') {
            return Err("YAML 使用 tab 縮排，無法精確定位 template".to_string());
        }
        result.push(Line {
            start,
            end,
            content,
            indent,
        });
        start = end;
    }
    Ok(result)
}

fn meaningful(line: &Line) -> bool {
    let content = line.content.trim();
    !content.is_empty() && !content.starts_with('#')
}

fn map_key(line: &Line) -> Result<Option<String>, String> {
    if !meaningful(line) {
        return Ok(None);
    }
    let body = &line.content[line.indent..];
    let Some(colon) = top_level_colon(body) else {
        return Ok(None);
    };
    let raw_key = body[..colon].trim();
    if raw_key.is_empty() {
        return Ok(None);
    }
    let fragment = format!("{raw_key}: null\n");
    let parsed =
        safe_load(&fragment).map_err(|error| format!("無法定位 YAML mapping key：{error}"))?;
    let Some(map) = as_map(&parsed) else {
        return Ok(None);
    };
    Ok(map.0.first().map(|(key, _)| py_str_of(key)))
}

fn top_level_colon(input: &str) -> Option<usize> {
    let mut single = false;
    let mut double = false;
    let mut escaped = false;
    let mut depth = 0i32;
    for (index, ch) in input.char_indices() {
        if double && escaped {
            escaped = false;
            continue;
        }
        if double && ch == '\\' {
            escaped = true;
            continue;
        }
        match ch {
            '\'' if !double => single = !single,
            '"' if !single => double = !double,
            '[' | '{' if !single && !double => depth += 1,
            ']' | '}' if !single && !double => depth -= 1,
            ':' if !single && !double && depth == 0 => {
                let next = input[index + ch.len_utf8()..].chars().next();
                if next.is_none_or(char::is_whitespace) {
                    return Some(index);
                }
            }
            _ => {}
        }
    }
    None
}

fn locate_insertion(
    text: &str,
    lines: &[Line],
    source: &SourceTemplate,
) -> Result<(usize, usize), String> {
    match source {
        SourceTemplate::Root => locate_root_insertion(lines),
        SourceTemplate::Mapping(name) => locate_profile_mapping(text, lines, name),
        SourceTemplate::Sequence(index) => locate_profile_sequence(lines, *index),
    }
}

fn profiles_line(lines: &[Line]) -> Result<(usize, usize), String> {
    let mut found = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        if line.indent == 0 && map_key(line)?.as_deref() == Some("profiles") {
            found.push(index);
        }
    }
    match found.as_slice() {
        [index] => Ok((*index, lines[*index].indent)),
        [] => Err("找不到 profiles mapping".to_string()),
        _ => Err("profiles mapping 重複，拒絕不確定遷移".to_string()),
    }
}

fn child_indent(
    lines: &[Line],
    parent_index: usize,
    parent_indent: usize,
) -> Result<usize, String> {
    for line in &lines[parent_index + 1..] {
        if meaningful(line) {
            if line.indent <= parent_indent {
                break;
            }
            return Ok(line.indent);
        }
    }
    Err("profiles 沒有可定位的 block template".to_string())
}

fn locate_profile_mapping(
    text: &str,
    lines: &[Line],
    name: &str,
) -> Result<(usize, usize), String> {
    let (parent, parent_indent) = profiles_line(lines)?;
    let body = &lines[parent].content[lines[parent].indent..];
    let colon = top_level_colon(body).ok_or_else(|| "profiles mapping 不完整".to_string())?;
    if body[colon + 1..].trim_start().starts_with(['{', '[', '*']) {
        return Err("flow-style 或 alias profiles 無法安全插入".to_string());
    }
    let indent = child_indent(lines, parent, parent_indent)?;
    let mut found = Vec::new();
    for (index, line) in lines.iter().enumerate().skip(parent + 1) {
        if meaningful(line) && line.indent <= parent_indent {
            break;
        }
        if meaningful(line)
            && line.indent == indent
            && !line.content[indent..].starts_with('-')
            && map_key(line)?.as_deref() == Some(name)
        {
            found.push(index);
        }
    }
    match found.as_slice() {
        [index] => {
            let line = &lines[*index];
            let body = &line.content[line.indent..];
            let colon =
                top_level_colon(body).ok_or_else(|| "template mapping 不完整".to_string())?;
            let value = body[colon + 1..].trim_start();
            if value.starts_with(['{', '[', '*']) {
                return Err(format!(
                    "template {name} 使用 flow mapping 或 alias，拒絕改寫"
                ));
            }
            locate_block_end(text, lines, *index, indent, indent + 2)
        }
        [] => Err(format!("找不到 template {name} 的原始位置")),
        _ => Err(format!("template {name} 重複，拒絕不確定遷移")),
    }
}

fn locate_profile_sequence(lines: &[Line], target: usize) -> Result<(usize, usize), String> {
    let (parent, parent_indent) = profiles_line(lines)?;
    let body = &lines[parent].content[lines[parent].indent..];
    let colon = top_level_colon(body).ok_or_else(|| "profiles sequence 不完整".to_string())?;
    if body[colon + 1..].trim_start().starts_with(['[', '{', '*']) {
        return Err("flow-style 或 alias profiles 無法安全插入".to_string());
    }
    let indent = child_indent(lines, parent, parent_indent)?;
    let items = lines
        .iter()
        .enumerate()
        .skip(parent + 1)
        .take_while(|(_, line)| !meaningful(line) || line.indent > parent_indent)
        .filter(|(_, line)| {
            meaningful(line) && line.indent == indent && is_sequence_entry(&line.content[indent..])
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let index = *items
        .get(target)
        .ok_or_else(|| format!("找不到 profiles list 第 {target} 個 template"))?;
    let line = &lines[index];
    let tail = sequence_tail(&line.content[indent..]);
    if tail.trim_start().starts_with(['{', '[', '*']) {
        return Err(format!(
            "profiles list 第 {target} 個 template 使用 flow mapping 或 alias，拒絕改寫"
        ));
    }
    locate_block_end_from_sequence(lines, index, indent, indent + 2)
}

fn locate_root_insertion(lines: &[Line]) -> Result<(usize, usize), String> {
    let mut first_target = None;
    let mut last_root = None;
    for (index, line) in lines.iter().enumerate() {
        if !meaningful(line) || line.indent != 0 {
            continue;
        }
        match map_key(line)?.as_deref() {
            Some("targets") => {
                first_target = Some(index);
                break;
            }
            Some(_) => last_root = Some(index),
            None => {}
        }
    }
    let position = if let Some(index) = first_target {
        lines[index].start
    } else if last_root.is_some() {
        text_end(lines)
    } else {
        return Err("頂層 template 沒有可定位的 mapping".to_string());
    };
    Ok((position, 0))
}

fn text_end(lines: &[Line]) -> usize {
    lines.last().map(|line| line.end).unwrap_or(0)
}

fn locate_block_end(
    text: &str,
    lines: &[Line],
    start: usize,
    parent_indent: usize,
    field_indent: usize,
) -> Result<(usize, usize), String> {
    let mut last_end = lines[start].end;
    for line in lines.iter().skip(start + 1) {
        if meaningful(line) && line.indent <= parent_indent {
            break;
        }
        if meaningful(line) {
            last_end = line.end;
        }
    }
    if last_end > text.len() {
        return Err("template 位置超出檔案".to_string());
    }
    Ok((last_end, field_indent))
}

fn locate_block_end_from_sequence(
    lines: &[Line],
    start: usize,
    item_indent: usize,
    field_indent: usize,
) -> Result<(usize, usize), String> {
    let mut last_end = lines[start].end;
    for line in lines.iter().skip(start + 1) {
        if meaningful(line)
            && line.indent == item_indent
            && is_sequence_entry(&line.content[item_indent..])
        {
            break;
        }
        if meaningful(line) && line.indent < item_indent {
            break;
        }
        if meaningful(line) {
            last_end = line.end;
        }
    }
    Ok((last_end, field_indent))
}

fn is_sequence_entry(content: &str) -> bool {
    content == "-" || content.starts_with("- ") || content.starts_with("-\t")
}

fn sequence_tail(content: &str) -> &str {
    content.strip_prefix('-').unwrap_or(content).trim_start()
}
