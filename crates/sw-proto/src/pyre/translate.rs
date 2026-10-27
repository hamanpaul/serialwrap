//! 將 Python re AST 轉成 fancy-regex 語法。

use std::collections::{BTreeMap, BTreeSet};

use super::parse::{Anchor, Category, CategoryKind, ClassItem, Expr, ParsedPattern, RepeatMode};
use super::unicode_tables;
use super::PyFlags;

const FLAG_I: u32 = PyFlags::I;
const FLAG_M: u32 = PyFlags::M;
const FLAG_S: u32 = PyFlags::S;
const FLAG_A: u32 = PyFlags::A;

pub(super) fn translate(parsed: &ParsedPattern) -> Result<String, String> {
    translate_inner(parsed, false)
}

/// UP-P7-02：保留全部擷取群組（engine 群組序號＝Python 群組序號），供 `PyRegex::search_groups`。
pub(super) fn translate_capturing(parsed: &ParsedPattern) -> Result<String, String> {
    translate_inner(parsed, true)
}

fn translate_inner(parsed: &ParsedPattern, keep_all_groups: bool) -> Result<String, String> {
    let mut used_groups = BTreeSet::new();
    collect_references(&parsed.expression, &mut used_groups);
    if keep_all_groups {
        collect_captures(&parsed.expression, &mut used_groups);
    }
    let group_map: BTreeMap<usize, usize> = used_groups
        .into_iter()
        .enumerate()
        .map(|(index, original)| (original, index + 1))
        .collect();
    let mut out = global_flag_prefix(parsed.global_flags);
    write_expr(
        &parsed.expression,
        &group_map,
        parsed.global_flags,
        &mut out,
    )?;
    Ok(out)
}

fn write_expr(
    expr: &Expr,
    group_map: &BTreeMap<usize, usize>,
    flags: u32,
    out: &mut String,
) -> Result<(), String> {
    match expr {
        Expr::Empty => {}
        Expr::Literal(ch) => {
            write_literal_set(*ch as u32, flags & FLAG_I != 0, flags & FLAG_A != 0, out)
        }
        Expr::Dot => out.push('.'),
        Expr::Category(category) => write_category(*category, false, out),
        Expr::CharacterClass { negated, items } => {
            out.push('[');
            if *negated {
                out.push('^');
            }
            for item in items {
                match item {
                    ClassItem::Character(ch) => {
                        write_codepoint_set(
                            &[(*ch as u32, *ch as u32)],
                            flags & FLAG_I != 0,
                            flags & FLAG_A != 0,
                            out,
                        );
                    }
                    ClassItem::Range(start, end) => {
                        write_codepoint_set(
                            &[(*start as u32, *end as u32)],
                            flags & FLAG_I != 0,
                            flags & FLAG_A != 0,
                            out,
                        );
                    }
                    ClassItem::Category(category) => write_category(*category, true, out),
                }
            }
            out.push(']');
        }
        Expr::Anchor(anchor) => match anchor {
            Anchor::Start => out.push('^'),
            Anchor::End { multiline: true } => out.push_str("(?=\\n|\\z)"),
            Anchor::End { multiline: false } => out.push_str("(?=\\n?\\z)"),
            Anchor::AbsoluteStart => out.push_str("\\A"),
            Anchor::AbsoluteEnd => out.push_str("\\z"),
        },
        Expr::WordBoundary { boundary, ascii } => write_word_boundary(*boundary, *ascii, out),
        Expr::Sequence(items) => {
            for item in items {
                write_expr(item, group_map, flags, out)?;
            }
        }
        Expr::Alternation(branches) => {
            for (index, branch) in branches.iter().enumerate() {
                if index > 0 {
                    out.push('|');
                }
                write_expr(branch, group_map, flags, out)?;
            }
        }
        Expr::NonCapture(body) => {
            out.push_str("(?:");
            write_expr(body, group_map, flags, out)?;
            out.push(')');
        }
        Expr::Capture { index, body } => {
            if group_map.contains_key(index) {
                out.push('(');
                write_expr(body, group_map, flags, out)?;
                out.push(')');
            } else if **body == Expr::Empty {
                return Ok(());
            } else if matches!(**body, Expr::Alternation(_)) {
                out.push_str("(?:");
                write_expr(body, group_map, flags, out)?;
                out.push(')');
            } else {
                write_expr(body, group_map, flags, out)?;
            }
        }
        Expr::Backreference { index } => {
            let index = group_map
                .get(index)
                .ok_or_else(|| "missing translated backreference".to_string())?;
            out.push('\\');
            out.push_str(&index.to_string());
        }
        Expr::Conditional { group, yes, no } => {
            let group = group_map
                .get(group)
                .ok_or_else(|| "missing translated conditional group".to_string())?;
            out.push_str("(?(");
            out.push_str(&group.to_string());
            out.push(')');
            write_expr(yes, group_map, flags, out)?;
            out.push('|');
            write_expr(no, group_map, flags, out)?;
            out.push(')');
        }
        Expr::Repeat {
            body,
            min,
            max,
            mode,
        } => write_repeat(body, *min, *max, mode, group_map, flags, out)?,
        Expr::LookAhead { positive, body } => {
            out.push_str(if *positive { "(?=" } else { "(?!" });
            write_expr(body, group_map, flags, out)?;
            out.push(')');
        }
        Expr::LookBehind { positive, body } => {
            out.push_str(if *positive { "(?<=" } else { "(?<!" });
            write_expr(body, group_map, flags, out)?;
            out.push(')');
        }
        Expr::Atomic(body) => {
            out.push_str("(?>");
            write_expr(body, group_map, flags, out)?;
            out.push(')');
        }
        Expr::ScopedFlags { add, remove, body } => {
            let scoped_flags = (flags | add) & !remove;
            let prefix = flag_prefix(*add, *remove);
            out.push_str("(?:");
            if !prefix.is_empty() {
                out.push_str(&prefix);
            }
            write_expr(body, group_map, scoped_flags, out)?;
            if !prefix.is_empty() {
                out.push(')');
            }
            out.push(')');
        }
    }
    Ok(())
}

fn write_repeat(
    body: &Expr,
    min: usize,
    max: Option<usize>,
    mode: &RepeatMode,
    group_map: &BTreeMap<usize, usize>,
    flags: u32,
    out: &mut String,
) -> Result<(), String> {
    if is_zero_width(body) {
        if min == 0 {
            // 零寬本體重複至多一次有意義；本體含保留的擷取群組時不得省略，否則後續群組序號錯位
            // （CPython：`()?` 參與為 ''、`()??` 不參與為 None）。
            if !has_mapped_capture(body, group_map) {
                return Ok(());
            }
            let possessive = matches!(mode, RepeatMode::Possessive);
            out.push_str(if possessive { "(?>(?:" } else { "(?:" });
            write_expr(body, group_map, flags, out)?;
            out.push_str(")?");
            if matches!(mode, RepeatMode::Lazy) {
                out.push('?');
            }
            if possessive {
                out.push(')');
            }
            return Ok(());
        }
        return write_expr(body, group_map, flags, out);
    }
    if min > super::ENGINE_REPEAT_LIMIT
        || max.is_some_and(|count| count > super::ENGINE_REPEAT_LIMIT)
    {
        return Err("large-repeat".to_string());
    }

    let possessive = matches!(mode, RepeatMode::Possessive);
    if possessive {
        out.push_str("(?>(?>");
    }
    out.push_str("(?:");
    write_expr(body, group_map, flags, out)?;
    out.push(')');
    if possessive {
        out.push(')');
    }
    write_repeat_bounds(min, max, out);
    if matches!(mode, RepeatMode::Lazy) {
        out.push('?');
    }
    if possessive {
        out.push(')');
    }
    Ok(())
}

fn write_repeat_bounds(min: usize, max: Option<usize>, out: &mut String) {
    match (min, max) {
        (0, None) => out.push('*'),
        (1, None) => out.push('+'),
        (0, Some(1)) => out.push('?'),
        (minimum, Some(maximum)) if minimum == maximum => {
            out.push('{');
            out.push_str(&minimum.to_string());
            out.push('}');
        }
        (minimum, Some(maximum)) => {
            out.push('{');
            out.push_str(&minimum.to_string());
            out.push(',');
            out.push_str(&maximum.to_string());
            out.push('}');
        }
        (minimum, None) => {
            out.push('{');
            out.push_str(&minimum.to_string());
            out.push_str(",}");
        }
    }
}

fn is_zero_width(expr: &Expr) -> bool {
    match expr {
        Expr::Empty
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. }
        | Expr::LookAhead { .. }
        | Expr::LookBehind { .. } => true,
        Expr::Sequence(items) | Expr::Alternation(items) => items.iter().all(is_zero_width),
        Expr::NonCapture(body)
        | Expr::Capture { body, .. }
        | Expr::Atomic(body)
        | Expr::ScopedFlags { body, .. }
        | Expr::Repeat { body, .. } => is_zero_width(body),
        Expr::Conditional { yes, no, .. } => is_zero_width(yes) && is_zero_width(no),
        Expr::Literal(_)
        | Expr::Dot
        | Expr::Category(_)
        | Expr::CharacterClass { .. }
        | Expr::Backreference { .. } => false,
    }
}

fn has_mapped_capture(expr: &Expr, group_map: &BTreeMap<usize, usize>) -> bool {
    let mut found = BTreeSet::new();
    collect_captures(expr, &mut found);
    found.iter().any(|index| group_map.contains_key(index))
}

fn collect_captures(expr: &Expr, used: &mut BTreeSet<usize>) {
    match expr {
        Expr::Capture { index, body } => {
            used.insert(*index);
            collect_captures(body, used);
        }
        Expr::Conditional { yes, no, .. } => {
            collect_captures(yes, used);
            collect_captures(no, used);
        }
        Expr::Sequence(items) | Expr::Alternation(items) => {
            for item in items {
                collect_captures(item, used);
            }
        }
        Expr::NonCapture(body)
        | Expr::Atomic(body)
        | Expr::ScopedFlags { body, .. }
        | Expr::Repeat { body, .. }
        | Expr::LookAhead { body, .. }
        | Expr::LookBehind { body, .. } => collect_captures(body, used),
        Expr::Backreference { .. }
        | Expr::Empty
        | Expr::Literal(_)
        | Expr::Dot
        | Expr::Category(_)
        | Expr::CharacterClass { .. }
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. } => {}
    }
}

fn collect_references(expr: &Expr, used: &mut BTreeSet<usize>) {
    match expr {
        Expr::Backreference { index } => {
            used.insert(*index);
        }
        Expr::Conditional { group, yes, no } => {
            used.insert(*group);
            collect_references(yes, used);
            collect_references(no, used);
        }
        Expr::Sequence(items) | Expr::Alternation(items) => {
            for item in items {
                collect_references(item, used);
            }
        }
        Expr::NonCapture(body)
        | Expr::Capture { body, .. }
        | Expr::Atomic(body)
        | Expr::ScopedFlags { body, .. }
        | Expr::Repeat { body, .. }
        | Expr::LookAhead { body, .. }
        | Expr::LookBehind { body, .. } => collect_references(body, used),
        Expr::Empty
        | Expr::Literal(_)
        | Expr::Dot
        | Expr::Category(_)
        | Expr::CharacterClass { .. }
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. } => {}
    }
}

fn write_literal_set(codepoint: u32, ignore_case: bool, ascii: bool, out: &mut String) {
    let members = if ignore_case {
        python_case_equivalents(codepoint, ascii)
    } else {
        vec![codepoint]
    };
    let ranges = merge_ranges(members.into_iter().map(|value| (value, value)).collect());
    if ranges.len() > 1 || ranges.first().is_some_and(|range| range.0 != range.1) {
        out.push('[');
        write_ranges(&ranges, out);
        out.push(']');
    } else {
        write_ranges(&ranges, out);
    }
}

fn python_case_equivalents(codepoint: u32, ascii: bool) -> Vec<u32> {
    if ascii {
        if let Some(ch) = char::from_u32(codepoint).filter(|ch| ch.is_ascii_alphabetic()) {
            return vec![
                ch.to_ascii_lowercase() as u32,
                ch.to_ascii_uppercase() as u32,
            ];
        }
        return vec![codepoint];
    }
    unicode_tables::case_group(codepoint).map_or_else(|| vec![codepoint], <[u32]>::to_vec)
}

fn write_codepoint_set(ranges: &[(u32, u32)], ignore_case: bool, ascii: bool, out: &mut String) {
    let ranges = if ignore_case {
        casefold_ranges(ranges, ascii)
    } else {
        merge_ranges(ranges.to_vec())
    };
    write_ranges(&ranges, out);
}

fn casefold_ranges(ranges: &[(u32, u32)], ascii: bool) -> Vec<(u32, u32)> {
    let mut expanded = ranges.to_vec();
    if ascii {
        for (start, end) in ranges {
            for codepoint in *start..=*end {
                if (b'a' as u32..=b'z' as u32).contains(&codepoint) {
                    expanded.push((codepoint - 32, codepoint - 32));
                } else if (b'A' as u32..=b'Z' as u32).contains(&codepoint) {
                    expanded.push((codepoint + 32, codepoint + 32));
                }
            }
        }
        return merge_ranges(expanded);
    }

    for &(member, group_index) in unicode_tables::CASE_MEMBER_GROUPS {
        if ranges
            .iter()
            .any(|(start, end)| member >= *start && member <= *end)
        {
            if let Some(group) = unicode_tables::CASE_GROUPS.get(group_index as usize) {
                expanded.extend(group.iter().map(|value| (*value, *value)));
            }
        }
    }
    merge_ranges(expanded)
}

fn merge_ranges(mut ranges: Vec<(u32, u32)>) -> Vec<(u32, u32)> {
    ranges.sort_unstable();
    let mut merged: Vec<(u32, u32)> = Vec::with_capacity(ranges.len());
    for (start, end) in ranges {
        if let Some(last) = merged.last_mut() {
            if start <= last.1.saturating_add(1) {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }
    merged
}

fn write_ranges(ranges: &[(u32, u32)], out: &mut String) {
    for &(start, end) in ranges {
        write_codepoint(start, out);
        if end != start {
            out.push('-');
            write_codepoint(end, out);
        }
    }
}

fn write_codepoint(codepoint: u32, out: &mut String) {
    out.push_str("\\x{");
    out.push_str(&format!("{codepoint:x}"));
    out.push('}');
}

fn category_ranges(category: Category) -> &'static [(u32, u32)] {
    match category.kind {
        CategoryKind::Digit => unicode_tables::DECIMAL_RANGES,
        CategoryKind::Space => unicode_tables::SPACE_RANGES,
        CategoryKind::Word => unicode_tables::ALNUM_RANGES,
    }
}

fn write_category(category: Category, inside_class: bool, out: &mut String) {
    let negate = category.negated;
    if !inside_class {
        out.push('[');
        if negate {
            out.push('^');
        }
        write_category_members(category, out);
        out.push(']');
    } else if negate {
        out.push_str("[^");
        write_category_members(category, out);
        out.push(']');
    } else {
        write_category_members(category, out);
    }
}

fn write_category_members(category: Category, out: &mut String) {
    if category.ascii {
        let ranges = match (category.kind, category.negated) {
            (CategoryKind::Digit, _) => &[(b'0' as u32, b'9' as u32)][..],
            (CategoryKind::Space, _) => &[(0x9, 0xd), (0x20, 0x20)][..],
            (CategoryKind::Word, _) => &[
                (b'0' as u32, b'9' as u32),
                (b'A' as u32, b'Z' as u32),
                (b'_' as u32, b'_' as u32),
                (b'a' as u32, b'z' as u32),
            ][..],
        };
        write_ranges(ranges, out);
    } else {
        write_ranges(category_ranges(category), out);
        if category.kind == CategoryKind::Word {
            write_codepoint(b'_' as u32, out);
        }
    }
}

fn write_word_boundary(boundary: bool, ascii: bool, out: &mut String) {
    let word_ranges: &[(u32, u32)] = if ascii {
        &[
            (b'0' as u32, b'9' as u32),
            (b'A' as u32, b'Z' as u32),
            (b'_' as u32, b'_' as u32),
            (b'a' as u32, b'z' as u32),
        ]
    } else {
        unicode_tables::ALNUM_RANGES
    };
    let mut word = String::from("[");
    write_ranges(word_ranges, &mut word);
    if !ascii {
        write_codepoint(b'_' as u32, &mut word);
    }
    word.push(']');
    if boundary {
        out.push_str("(?:(?<=");
        out.push_str(&word);
        out.push_str(")(?!");
        out.push_str(&word);
        out.push_str(")|(?<!");
        out.push_str(&word);
        out.push_str(")(?=");
        out.push_str(&word);
        out.push_str("))");
    } else {
        out.push_str("(?!\\A\\z)(?:(?<=");
        out.push_str(&word);
        out.push_str(")(?=");
        out.push_str(&word);
        out.push_str(")|(?<!");
        out.push_str(&word);
        out.push_str(")(?!");
        out.push_str(&word);
        out.push_str("))");
    }
}

fn flag_prefix(add: u32, remove: u32) -> String {
    let mut enabled = String::new();
    let mut disabled = String::new();
    for (bit, flag) in [(FLAG_M, 'm'), (FLAG_S, 's')] {
        if add & bit != 0 {
            enabled.push(flag);
        }
        if remove & bit != 0 {
            disabled.push(flag);
        }
    }
    if enabled.is_empty() && disabled.is_empty() {
        String::new()
    } else if disabled.is_empty() {
        format!("(?{enabled}:")
    } else if enabled.is_empty() {
        format!("(?-{disabled}:")
    } else {
        format!("(?{enabled}-{disabled}:")
    }
}

fn global_flag_prefix(flags: u32) -> String {
    let mut enabled = String::new();
    for (bit, flag) in [(FLAG_M, 'm'), (FLAG_S, 's')] {
        if flags & bit != 0 {
            enabled.push(flag);
        }
    }
    if enabled.is_empty() {
        String::new()
    } else {
        format!("(?{enabled})")
    }
}
