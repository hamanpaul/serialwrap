//! YAML 輸出：移植 PyYAML 6 `safe_dump(default_flow_style=False)` 的標量與區塊表示規則。

use super::{resolve_plain, YMap, YValue};
use crate::pyjson::PyInt;

const WIDTH: usize = 80;

/// 等同 PyYAML `yaml.safe_dump(v, allow_unicode=..., sort_keys=..., default_flow_style=False)`。
pub fn safe_dump(value: &YValue, allow_unicode: bool, sort_keys: bool) -> String {
    match value {
        YValue::Map(map) => emit_map(map, 0, sort_keys, allow_unicode),
        YValue::Seq(items) => emit_sequence(items, 0, sort_keys, allow_unicode),
        YValue::Set(items) => emit_set(items, 0, allow_unicode, sort_keys),
        YValue::Binary(bytes) => emit_binary(bytes),
        _ => {
            let mut output = scalar(value, allow_unicode, 2, 0, false);
            output.push('\n');
            if !matches!(value, YValue::Str(text) if needs_double_quotes(text, allow_unicode) || needs_single_quotes(text))
            {
                output.push_str("...\n");
            }
            output
        }
    }
}

fn emit_map(map: &YMap, indent: usize, sort_keys: bool, allow_unicode: bool) -> String {
    if map.0.is_empty() {
        return format!("{}{{}}\n", spaces(indent));
    }
    let entries = ordered_pairs(map, sort_keys);
    let mut output = String::new();
    for (key, value) in entries {
        output.push_str(&emit_map_pair(
            key,
            value,
            indent,
            &spaces(indent),
            sort_keys,
            allow_unicode,
        ));
    }
    output
}

fn ordered_pairs(map: &YMap, sort_keys: bool) -> Vec<(&YValue, &YValue)> {
    let mut pairs: Vec<_> = map.0.iter().map(|(key, value)| (key, value)).collect();
    if sort_keys {
        let comparable = pairs.iter().all(|(left, _)| {
            pairs
                .iter()
                .all(|(right, _)| mapping_key_order(left, right).is_some())
        });
        if comparable {
            pairs.sort_by(|(left, _), (right, _)| {
                mapping_key_order(left, right).unwrap_or(std::cmp::Ordering::Equal)
            });
        }
    }
    pairs
}

fn mapping_key_order(left: &YValue, right: &YValue) -> Option<std::cmp::Ordering> {
    match (left, right) {
        (YValue::Str(a), YValue::Str(b)) => Some(a.cmp(b)),
        (YValue::Bool(a), YValue::Bool(b)) => Some(a.cmp(b)),
        (YValue::Int(a), YValue::Int(b)) => Some(integer_order(a, b)),
        (YValue::Float(a), YValue::Float(b)) => {
            Some(a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
        }
        (YValue::Timestamp { py_str: a }, YValue::Timestamp { py_str: b }) => Some(a.cmp(b)),
        (YValue::Binary(a), YValue::Binary(b)) => Some(a.cmp(b)),
        (YValue::Bool(a), YValue::Int(b)) => Some(integer_order(&PyInt::Small(i128::from(*a)), b)),
        (YValue::Int(a), YValue::Bool(b)) => Some(integer_order(a, &PyInt::Small(i128::from(*b)))),
        (YValue::Bool(a), YValue::Float(b)) => Some(f64::from(u8::from(*a)).total_cmp(b)),
        (YValue::Float(a), YValue::Bool(b)) => Some(a.total_cmp(&f64::from(u8::from(*b)))),
        (YValue::Int(a), YValue::Float(b)) => Some(integer_float_order(a, *b)),
        (YValue::Float(a), YValue::Int(b)) => Some(integer_float_order(b, *a).reverse()),
        _ => None,
    }
}

fn integer_float_order(integer: &PyInt, float: f64) -> std::cmp::Ordering {
    if float.is_nan() {
        return std::cmp::Ordering::Equal;
    }
    if float == f64::INFINITY {
        return std::cmp::Ordering::Less;
    }
    if float == f64::NEG_INFINITY {
        return std::cmp::Ordering::Greater;
    }
    let whole = if float.is_sign_negative() {
        float.ceil()
    } else {
        float.floor()
    };
    let whole = if whole == 0.0 {
        "0".to_string()
    } else {
        format!("{whole:.0}")
    };
    match integer_order(integer, &PyInt::Big(whole)) {
        std::cmp::Ordering::Equal if float.fract() != 0.0 => {
            if float.is_sign_negative() {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Less
            }
        }
        order => order,
    }
}

fn emit_map_pair(
    key: &YValue,
    value: &YValue,
    key_indent: usize,
    prefix: &str,
    sort_keys: bool,
    allow_unicode: bool,
) -> String {
    let rendered_key = scalar(key, allow_unicode, key_indent + 2, prefix.len() + 2, true);
    if key_requires_explicit_form(key, &rendered_key, prefix.len()) {
        let mut output = format!("{prefix}? {rendered_key}\n{}:", spaces(key_indent));
        append_explicit_key_value(
            &mut output,
            value,
            key_indent,
            sort_keys,
            allow_unicode,
        );
        return output;
    }

    let colon_prefix = format!("{prefix}{rendered_key}:");
    let mut output = colon_prefix;
    append_map_value(
        &mut output,
        value,
        key_indent,
        sort_keys,
        allow_unicode,
        prefix.len() + rendered_key.chars().count() + 2,
    );
    output
}

fn key_requires_explicit_form(key: &YValue, rendered_key: &str, prefix_len: usize) -> bool {
    matches!(key, YValue::Str(text) if text.is_empty() || text.chars().any(is_break))
        || rendered_key.chars().count() + prefix_len + 5 >= 128
}

fn append_explicit_key_value(
    output: &mut String,
    value: &YValue,
    key_indent: usize,
    sort_keys: bool,
    allow_unicode: bool,
) {
    match value {
        YValue::Map(map) if !map.0.is_empty() => {
            output.push(' ');
            let pairs = ordered_pairs(map, sort_keys);
            if let Some((key, value)) = pairs.first() {
                output.push_str(&emit_map_pair(
                    key,
                    value,
                    key_indent + 2,
                    "",
                    sort_keys,
                    allow_unicode,
                ));
            }
            for (key, value) in pairs.into_iter().skip(1) {
                output.push_str(&emit_map_pair(
                    key,
                    value,
                    key_indent + 2,
                    &spaces(key_indent + 2),
                    sort_keys,
                    allow_unicode,
                ));
            }
        }
        YValue::Seq(items) if !items.is_empty() => {
            let Some((first, rest)) = items.split_first() else {
                unreachable!();
            };
            output.push(' ');
            let first_line = emit_sequence_item(first, key_indent + 2, sort_keys, allow_unicode);
            output.push_str(
                first_line
                    .strip_prefix(&spaces(key_indent + 2))
                    .unwrap_or(&first_line),
            );
            for item in rest {
                output.push_str(&emit_sequence_item(
                    item,
                    key_indent + 2,
                    sort_keys,
                    allow_unicode,
                ));
            }
        }
        _ => append_map_value(
            output,
            value,
            key_indent,
            sort_keys,
            allow_unicode,
            key_indent + 2,
        ),
    }
}

fn append_map_value(
    output: &mut String,
    value: &YValue,
    key_indent: usize,
    sort_keys: bool,
    allow_unicode: bool,
    first_column: usize,
) {
    match value {
        YValue::Map(map) if map.0.is_empty() => output.push_str(" {}\n"),
        YValue::Seq(items) if items.is_empty() => output.push_str(" []\n"),
        YValue::Map(map) => {
            output.push('\n');
            output.push_str(&emit_map(map, key_indent + 2, sort_keys, allow_unicode));
        }
        YValue::Seq(items) => {
            output.push('\n');
            output.push_str(&emit_sequence(items, key_indent, sort_keys, allow_unicode));
        }
        YValue::Set(items) if items.is_empty() => output.push_str(" !!set {}\n"),
        YValue::Set(items) => {
            output.push_str(" !!set\n");
            output.push_str(&emit_set_entries(
                items,
                key_indent + 2,
                allow_unicode,
                sort_keys,
            ));
        }
        YValue::Binary(bytes) if bytes.is_empty() => output.push_str(" !!binary \"\"\n"),
        YValue::Binary(bytes) => {
            output.push_str(" !!binary |\n");
            output.push_str(&binary_body(bytes, key_indent + 2));
        }
        _ => {
            output.push(' ');
            output.push_str(&scalar(
                value,
                allow_unicode,
                key_indent + 2,
                first_column,
                false,
            ));
            output.push('\n');
        }
    }
}

fn emit_sequence(items: &[YValue], indent: usize, sort_keys: bool, allow_unicode: bool) -> String {
    if items.is_empty() {
        return format!("{}[]\n", spaces(indent));
    }
    let mut output = String::new();
    for item in items {
        output.push_str(&emit_sequence_item(item, indent, sort_keys, allow_unicode));
    }
    output
}

fn emit_sequence_item(
    item: &YValue,
    indent: usize,
    sort_keys: bool,
    allow_unicode: bool,
) -> String {
    let prefix = format!("{}- ", spaces(indent));
    match item {
        YValue::Map(map) if map.0.is_empty() => format!("{prefix}{{}}\n"),
        YValue::Seq(items) if items.is_empty() => format!("{prefix}[]\n"),
        YValue::Map(map) => emit_map_item(map, indent, sort_keys, allow_unicode),
        YValue::Seq(items) => {
            emit_nested_sequence(items, indent, &prefix, sort_keys, allow_unicode)
        }
        YValue::Set(items) if items.is_empty() => format!("{prefix}!!set {{}}\n"),
        YValue::Set(items) => {
            let mut output = format!("{prefix}!!set\n");
            output.push_str(&emit_set_entries(
                items,
                indent + 2,
                allow_unicode,
                sort_keys,
            ));
            output
        }
        YValue::Binary(bytes) if bytes.is_empty() => format!("{prefix}!!binary \"\"\n"),
        YValue::Binary(bytes) => {
            let mut output = format!("{prefix}!!binary |\n");
            output.push_str(&binary_body(bytes, indent + 2));
            output
        }
        _ => {
            let mut output = prefix.clone();
            output.push_str(&scalar(
                item,
                allow_unicode,
                indent + 2,
                prefix.chars().count(),
                false,
            ));
            output.push('\n');
            output
        }
    }
}

fn emit_map_item(map: &YMap, indent: usize, sort_keys: bool, allow_unicode: bool) -> String {
    let pairs = ordered_pairs(map, sort_keys);
    let Some((first_key, first_value)) = pairs.first() else {
        return format!("{}- {{}}\n", spaces(indent));
    };
    let first_prefix = format!("{}- ", spaces(indent));
    let mut output = emit_map_pair(
        first_key,
        first_value,
        indent + 2,
        &first_prefix,
        sort_keys,
        allow_unicode,
    );
    for (key, value) in pairs.into_iter().skip(1) {
        output.push_str(&emit_map_pair(
            key,
            value,
            indent + 2,
            &spaces(indent + 2),
            sort_keys,
            allow_unicode,
        ));
    }
    output
}

fn emit_nested_sequence(
    items: &[YValue],
    indent: usize,
    prefix: &str,
    sort_keys: bool,
    allow_unicode: bool,
) -> String {
    let Some((first, rest)) = items.split_first() else {
        return format!("{prefix}[]\n");
    };
    let inner = indent + 2;
    let lead = format!("{prefix}- ");
    let mut output = String::new();
    output.push_str(&emit_first_sequence_value(
        first,
        &lead,
        inner,
        sort_keys,
        allow_unicode,
    ));
    for item in rest {
        output.push_str(&emit_sequence_item(
            item,
            inner,
            sort_keys,
            allow_unicode,
        ));
    }
    output
}

fn emit_first_sequence_value(
    value: &YValue,
    lead: &str,
    inner: usize,
    sort_keys: bool,
    allow_unicode: bool,
) -> String {
    match value {
        YValue::Map(map) if map.0.is_empty() => format!("{lead}{{}}\n"),
        YValue::Seq(items) if items.is_empty() => format!("{lead}[]\n"),
        YValue::Map(map) => {
            let pairs = ordered_pairs(map, sort_keys);
            let mut output = String::new();
            if let Some((key, value)) = pairs.first() {
                output.push_str(&emit_map_pair(
                    key,
                    value,
                    inner + 2,
                    lead,
                    sort_keys,
                    allow_unicode,
                ));
            }
            for (key, value) in pairs.into_iter().skip(1) {
                output.push_str(&emit_map_pair(
                    key,
                    value,
                    inner + 2,
                    &spaces(inner + 2),
                    sort_keys,
                    allow_unicode,
                ));
            }
            output
        }
        YValue::Seq(items) => emit_nested_sequence(items, inner, lead, sort_keys, allow_unicode),
        YValue::Set(items) if items.is_empty() => format!("{lead}!!set {{}}\n"),
        YValue::Set(items) => {
            let mut output = format!("{lead}!!set\n");
            output.push_str(&emit_set_entries(
                items,
                inner + 2,
                allow_unicode,
                sort_keys,
            ));
            output
        }
        YValue::Binary(bytes) if bytes.is_empty() => format!("{lead}!!binary \"\"\n"),
        YValue::Binary(bytes) => {
            let mut output = format!("{lead}!!binary |\n");
            output.push_str(&binary_body(bytes, inner + 2));
            output
        }
        _ => {
            let mut output = lead.to_string();
            output.push_str(&scalar(value, allow_unicode, inner + 2, lead.chars().count(), false));
            output.push('\n');
            output
        }
    }
}

fn emit_set(items: &[YValue], indent: usize, allow_unicode: bool, sort_keys: bool) -> String {
    if items.is_empty() {
        return format!("{}!!set {{}}\n", spaces(indent));
    }
    let mut output = format!("{}!!set\n", spaces(indent));
    output.push_str(&emit_set_entries(items, indent, allow_unicode, sort_keys));
    output
}

fn emit_set_entries(
    items: &[YValue],
    indent: usize,
    allow_unicode: bool,
    sort_keys: bool,
) -> String {
    let mut values: Vec<&YValue> = items.iter().collect();
    if sort_keys {
        values.sort_by(|left, right| set_order(left, right, allow_unicode, indent));
    }
    let mut output = String::new();
    for value in values {
        let rendered = scalar(value, allow_unicode, indent, indent, true);
        if matches!(value, YValue::Str(text) if text.is_empty()) {
            output.push_str(&format!(
                "{}? {rendered}\n{}: null\n",
                spaces(indent),
                spaces(indent)
            ));
        } else {
            output.push_str(&format!("{}{rendered}: null\n", spaces(indent)));
        }
    }
    output
}

fn set_order(
    left: &YValue,
    right: &YValue,
    allow_unicode: bool,
    indent: usize,
) -> std::cmp::Ordering {
    match (left, right) {
        (YValue::Str(a), YValue::Str(b)) => a.cmp(b),
        (YValue::Bool(a), YValue::Bool(b)) => a.cmp(b),
        (YValue::Int(a), YValue::Int(b)) => integer_order(a, b),
        (YValue::Float(a), YValue::Float(b)) => a.total_cmp(b),
        (YValue::Timestamp { py_str: a }, YValue::Timestamp { py_str: b }) => a.cmp(b),
        (YValue::Binary(a), YValue::Binary(b)) => a.cmp(b),
        _ => scalar(left, allow_unicode, indent, indent, true).cmp(&scalar(
            right,
            allow_unicode,
            indent,
            indent,
            true,
        )),
    }
}

fn integer_order(left: &PyInt, right: &PyInt) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let left = match left {
        PyInt::Small(value) => value.to_string(),
        PyInt::Big(value) => value.clone(),
    };
    let right = match right {
        PyInt::Small(value) => value.to_string(),
        PyInt::Big(value) => value.clone(),
    };
    let (left_negative, left_magnitude) = decimal_parts(&left);
    let (right_negative, right_magnitude) = decimal_parts(&right);
    match (left_negative, right_negative) {
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
        (negative, _) => {
            let order = left_magnitude
                .len()
                .cmp(&right_magnitude.len())
                .then_with(|| left_magnitude.cmp(right_magnitude));
            if negative {
                order.reverse()
            } else {
                order
            }
        }
    }
}

fn decimal_parts(value: &str) -> (bool, &str) {
    match value.strip_prefix('-') {
        Some(magnitude) => (true, magnitude),
        None => (false, value),
    }
}

fn scalar(
    value: &YValue,
    allow_unicode: bool,
    continuation_indent: usize,
    first_column: usize,
    key: bool,
) -> String {
    match value {
        YValue::Null => "null".to_string(),
        YValue::Bool(true) => "true".to_string(),
        YValue::Bool(false) => "false".to_string(),
        YValue::Int(PyInt::Small(value)) => value.to_string(),
        YValue::Int(PyInt::Big(value)) => value.clone(),
        YValue::Float(value) => float_scalar(*value),
        YValue::Str(value) => {
            string_scalar(value, allow_unicode, continuation_indent, first_column, key)
        }
        YValue::Timestamp { py_str } => py_str.clone(),
        YValue::Binary(value) => format!("!!binary {}", base64(value)),
        YValue::Seq(values) if values.is_empty() => "[]".to_string(),
        YValue::Map(map) if map.0.is_empty() => "{}".to_string(),
        YValue::Set(values) if values.is_empty() => "!!set {}".to_string(),
        YValue::Seq(_) | YValue::Map(_) | YValue::Set(_) => "{}".to_string(),
    }
}

fn float_scalar(value: f64) -> String {
    if value.is_nan() {
        return ".nan".to_string();
    }
    if value == f64::INFINITY {
        return ".inf".to_string();
    }
    if value == f64::NEG_INFINITY {
        return "-.inf".to_string();
    }
    let mut text = crate::pyjson::repr_float(value);
    if let Some((mantissa, exponent)) = text.split_once('e') {
        let mantissa = if mantissa.contains('.') {
            mantissa.to_string()
        } else {
            format!("{mantissa}.0")
        };
        text = format!("{mantissa}e{exponent}");
    } else if !text.contains('.') {
        text.push_str(".0");
    }
    text
}

fn string_scalar(
    value: &str,
    allow_unicode: bool,
    continuation_indent: usize,
    first_column: usize,
    key: bool,
) -> String {
    if needs_double_quotes(value, allow_unicode) {
        return double_quoted(value, allow_unicode, continuation_indent, first_column, !key);
    }
    if needs_single_quotes(value) || (key && value.is_empty()) {
        return single_quoted(value, continuation_indent, first_column, !key);
    }
    wrap_plain(value, continuation_indent, first_column, !key)
}

fn needs_double_quotes(value: &str, allow_unicode: bool) -> bool {
    let chars: Vec<char> = value.chars().collect();
    let mut previous_space = false;
    let mut previous_break = false;
    for ch in chars.iter().copied() {
        let code = ch as u32;
        let unicode_character = ch == '\u{85}'
            || ('\u{a0}'..='\u{d7ff}').contains(&ch)
            || ('\u{e000}'..='\u{fffd}').contains(&ch)
            || (0x10000..0x10ffff).contains(&code);
        let printable_ascii = ch == '\n' || ('\u{20}'..='\u{7e}').contains(&ch);
        if !printable_ascii && !(allow_unicode && unicode_character && ch != '\u{feff}') {
            return true;
        }
        if !allow_unicode && unicode_character {
            return true;
        }
        let is_space = ch == ' ';
        let line_break = is_break(ch);
        if (is_space && previous_break) || (line_break && previous_space) {
            return true;
        }
        previous_space = is_space;
        previous_break = line_break;
    }
    false
}

fn needs_single_quotes(value: &str) -> bool {
    if value.is_empty()
        || value.starts_with(' ')
        || value.ends_with(' ')
        || value.chars().any(is_break)
        || resolve_plain(value).is_some()
        || value.starts_with("---")
        || value.starts_with("...")
    {
        return true;
    }
    let chars: Vec<char> = value.chars().collect();
    let first = chars[0];
    let followed_by_whitespace = chars.get(1).is_none_or(|ch| yaml_whitespace(*ch));
    let indicator = match first {
        '-' | '?' | ':' => followed_by_whitespace,
        '#' | ',' | '[' | ']' | '{' | '}' | '&' | '*' | '!' | '|' | '>' | '\'' | '"' | '%'
        | '@' | '`' => true,
        _ => false,
    };
    if indicator
        || value.ends_with(':')
        || chars
            .windows(2)
            .any(|pair| pair[0] == ':' && yaml_whitespace(pair[1]))
    {
        return true;
    }
    chars
        .windows(2)
        .any(|pair| pair[1] == '#' && yaml_whitespace(pair[0]))
}

fn yaml_whitespace(ch: char) -> bool {
    matches!(
        ch,
        '\0' | ' ' | '\t' | '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

fn wrap_plain(value: &str, continuation_indent: usize, first_column: usize, split: bool) -> String {
    let chars: Vec<char> = value.chars().collect();
    let mut output = String::new();
    let mut column = first_column;
    let mut start = 0usize;
    let mut end = 0usize;
    let mut spaces = false;
    while end <= chars.len() {
        let ch = chars.get(end).copied();
        if spaces {
            if ch != Some(' ') {
                if split && start + 1 == end && column > WIDTH {
                    append_indent(&mut output, continuation_indent, &mut column);
                } else {
                    append_chars(&mut output, &chars[start..end]);
                    column += end - start;
                }
                start = end;
            }
        } else if ch.is_none() || ch == Some(' ') {
            append_chars(&mut output, &chars[start..end]);
            column += end - start;
            start = end;
        }
        if ch.is_some() {
            spaces = ch == Some(' ');
        }
        end += 1;
    }
    output
}

fn single_quoted(
    value: &str,
    continuation_indent: usize,
    first_column: usize,
    split: bool,
) -> String {
    let chars: Vec<char> = value.chars().collect();
    let mut output = String::from("'");
    let mut column = first_column + 1;
    let mut start = 0usize;
    let mut end = 0usize;
    let mut spaces = false;
    let mut breaks = false;
    while end <= chars.len() {
        let ch = chars.get(end).copied();
        if spaces {
            if ch != Some(' ') {
                if split && start + 1 == end && column > WIDTH && start != 0 && end != chars.len() {
                    append_indent(&mut output, continuation_indent, &mut column);
                } else {
                    append_chars(&mut output, &chars[start..end]);
                    column += end - start;
                }
                start = end;
            }
        } else if breaks {
            if ch.is_none_or(|ch| !is_break(ch)) {
                if chars[start] == '\n' {
                    output.push('\n');
                }
                for br in &chars[start..end] {
                    if *br == '\n' {
                        output.push('\n');
                    } else {
                        output.push(*br);
                    }
                }
                output.push_str(&" ".repeat(continuation_indent));
                column = continuation_indent;
                start = end;
            }
        } else if ch.is_none_or(|ch| ch == ' ' || is_break(ch) || ch == '\'') && start < end {
            append_chars(&mut output, &chars[start..end]);
            column += end - start;
            start = end;
        }
        if ch == Some('\'') {
            output.push_str("''");
            column += 2;
            start = end + 1;
        }
        if ch.is_some() {
            spaces = ch == Some(' ');
            breaks = ch.is_some_and(is_break);
        }
        end += 1;
    }
    output.push('\'');
    output
}

fn double_quoted(
    value: &str,
    allow_unicode: bool,
    continuation_indent: usize,
    first_column: usize,
    split: bool,
) -> String {
    let chars: Vec<char> = value.chars().collect();
    let mut output = String::from("\"");
    let mut column = first_column + 1;
    let mut start = 0usize;
    let mut end = 0usize;
    while end <= chars.len() {
        let ch = chars.get(end).copied();
        if ch.is_none_or(|ch| double_escape(ch, allow_unicode).is_some()) {
            if start < end {
                append_chars(&mut output, &chars[start..end]);
                column += end - start;
                start = end;
            }
            if let Some(ch) = ch {
                let token = double_escape(ch, allow_unicode).expect("需跳脫的字元");
                output.push_str(&token);
                column += token.len();
                start = end + 1;
            }
        }
        if split
            && end > 0
            && end < chars.len().saturating_sub(1)
            && (ch == Some(' ') || start >= end)
            && column as isize + end as isize - start as isize > WIDTH as isize
        {
            if start < end {
                append_chars(&mut output, &chars[start..end]);
                output.push('\\');
                column += end - start + 1;
                start = end;
            } else {
                output.push('\\');
                column += 1;
            }
            append_indent(&mut output, continuation_indent, &mut column);
            if chars.get(start) == Some(&' ') {
                output.push('\\');
                column += 1;
            }
        }
        end += 1;
    }
    output.push('"');
    output
}

fn double_escape(ch: char, allow_unicode: bool) -> Option<String> {
    let code = ch as u32;
    let forced_escape = matches!(ch, '"' | '\\' | '\u{85}' | '\u{2028}' | '\u{2029}' | '\u{feff}');
    let allowed_unicode = ('\u{a0}'..='\u{d7ff}').contains(&ch)
        || ('\u{e000}'..='\u{fffd}').contains(&ch) && ch != '\u{feff}';
    let printable_ascii = ('\u{20}'..='\u{7e}').contains(&ch);
    if !forced_escape && (printable_ascii || allow_unicode && allowed_unicode) {
        return None;
    }

    let escaped = match ch {
        '\0' => Some("\\0".to_string()),
        '\x07' => Some("\\a".to_string()),
        '\x08' => Some("\\b".to_string()),
        '\t' => Some("\\t".to_string()),
        '\n' => Some("\\n".to_string()),
        '\x0b' => Some("\\v".to_string()),
        '\x0c' => Some("\\f".to_string()),
        '\r' => Some("\\r".to_string()),
        '\x1b' => Some("\\e".to_string()),
        '"' => Some("\\\"".to_string()),
        '\\' => Some("\\\\".to_string()),
        '\u{85}' => Some("\\N".to_string()),
        '\u{a0}' => Some("\\_".to_string()),
        '\u{2028}' => Some("\\L".to_string()),
        '\u{2029}' => Some("\\P".to_string()),
        _ => None,
    };
    if escaped.is_some() {
        return escaped;
    }
    Some(if code <= 0xff {
        format!("\\x{code:02X}")
    } else if code <= 0xffff {
        format!("\\u{code:04X}")
    } else {
        format!("\\U{code:08X}")
    })
}

fn append_chars(output: &mut String, chars: &[char]) {
    output.extend(chars.iter());
}

fn append_indent(output: &mut String, indent: usize, column: &mut usize) {
    output.push('\n');
    output.push_str(&spaces(indent));
    *column = indent;
}

fn is_break(ch: char) -> bool {
    matches!(ch, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

fn emit_binary(bytes: &[u8]) -> String {
    if bytes.is_empty() {
        String::from("!!binary \"\"\n")
    } else {
        let mut block = String::from("!!binary |\n");
        block.push_str(&binary_body(bytes, 2));
        block
    }
}

fn binary_body(bytes: &[u8], indent: usize) -> String {
    let encoded = base64(bytes);
    let mut output = String::new();
    if encoded.is_empty() {
        output.push_str(&spaces(indent));
        output.push('\n');
        return output;
    }
    for line in encoded.as_bytes().chunks(76) {
        output.push_str(&spaces(indent));
        output.push_str(std::str::from_utf8(line).unwrap_or_default());
        output.push('\n');
    }
    output
}

fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let a = chunk[0];
        let b = *chunk.get(1).unwrap_or(&0);
        let c = *chunk.get(2).unwrap_or(&0);
        output.push(TABLE[(a >> 2) as usize] as char);
        output.push(TABLE[(((a & 0x03) << 4) | (b >> 4)) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[(((b & 0x0f) << 2) | (c >> 6)) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(c & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    output
}

fn spaces(count: usize) -> String {
    " ".repeat(count)
}
