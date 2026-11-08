//! CPython argparse usage 行格式化。

use super::{
    action_metavar, display_actions, ActionKind, ActionSpec, MutexSpec, Nargs, ParserSpec,
};
use std::collections::HashSet;

fn value_taking(action: &ActionSpec) -> bool {
    !matches!(
        action.kind,
        ActionKind::Help | ActionKind::Version | ActionKind::StoreTrue | ActionKind::Count
    )
}

fn action_args(action: &ActionSpec) -> String {
    let metavar = action_metavar(action);
    match action.nargs {
        Nargs::One => metavar,
        Nargs::ZeroOrMore => format!("[{metavar} ...]"),
    }
}

fn action_usage(action: &ActionSpec, in_mutex: bool) -> Vec<String> {
    if action.option_strings.is_empty() {
        return vec![action_args(action)];
    }

    let option = action.option_strings[0];
    let parts = if value_taking(action) {
        vec![option.to_string(), action_args(action)]
    } else {
        vec![option.to_string()]
    };
    if !action.required && !in_mutex {
        return vec![format!("[{}]", parts.join(" "))];
    }
    parts
}

fn mutex_usage(group: &MutexSpec, actions: &[ActionSpec]) -> Option<(String, HashSet<String>)> {
    let members: Vec<&ActionSpec> = group
        .dests
        .iter()
        .filter_map(|dest| actions.iter().find(|action| action.dest == *dest))
        .collect();
    if members.is_empty() {
        return None;
    }
    let inner = members
        .iter()
        .map(|action| action_usage(action, true).join(" "))
        .collect::<Vec<_>>()
        .join(" | ");
    let text = if group.required {
        format!("({inner})")
    } else {
        format!("[{inner}]")
    };
    let dests = members
        .iter()
        .map(|action| action.dest.to_string())
        .collect();
    Some((text, dests))
}

fn usage_parts(spec: &ParserSpec, actions: &[ActionSpec], optionals: bool) -> Vec<String> {
    let mut ordered: Vec<&ActionSpec> = actions
        .iter()
        .filter(|action| !action.option_strings.is_empty() == optionals)
        .collect();
    let mut parts = Vec::new();
    let mut handled = HashSet::new();
    for action in ordered.drain(..) {
        if handled.contains(action.dest) {
            continue;
        }
        if let Some(group) = spec
            .mutex
            .iter()
            .find(|group| group.dests.iter().any(|dest| *dest == action.dest))
        {
            if let Some((part, dests)) = mutex_usage(group, actions) {
                // argparse 會把互斥群組放在第一個成員原本的位置。
                if group
                    .dests
                    .first()
                    .is_some_and(|first| *first == action.dest)
                {
                    parts.push(part);
                    handled.extend(dests);
                }
                continue;
            }
        }
        parts.extend(action_usage(action, false));
        handled.insert(action.dest.to_string());
    }
    parts
}

fn wrapped_usage(
    prefix: &str,
    prog: &str,
    optionals: &[String],
    positionals: &[String],
    width: usize,
) -> String {
    let usage = [optionals, positionals]
        .into_iter()
        .flat_map(|parts| parts.iter().cloned())
        .collect::<Vec<_>>()
        .join(" ");
    let full = if usage.is_empty() {
        prog.to_string()
    } else {
        format!("{prog} {usage}")
    };
    if prefix.chars().count() + full.chars().count() <= width {
        return format!("{prefix}{full}\n\n");
    }

    let prog_len = prog.chars().count();
    let prefix_len = prefix.chars().count();
    let opt_parts = usage_tokens(&optionals.join(" "));
    let pos_parts = usage_tokens(&positionals.join(" "));
    let mut lines = Vec::new();
    if (prefix_len + prog_len) as f64 <= width as f64 * 0.75 {
        let indent = " ".repeat(prefix_len + prog_len + 1);
        if !opt_parts.is_empty() {
            let mut first = vec![prog.to_string()];
            first.extend(opt_parts.clone());
            lines.extend(wrap_usage_parts(&first, &indent, Some(prefix), width));
            lines.extend(wrap_usage_parts(&pos_parts, &indent, None, width));
        } else if !pos_parts.is_empty() {
            let mut first = vec![prog.to_string()];
            first.extend(pos_parts.clone());
            lines.extend(wrap_usage_parts(&first, &indent, Some(prefix), width));
        } else {
            lines.push(prog.to_string());
        }
    } else {
        let indent = " ".repeat(prefix_len);
        let mut parts = opt_parts.clone();
        parts.extend(pos_parts.clone());
        let mut wrapped = wrap_usage_parts(&parts, &indent, None, width);
        if wrapped.len() > 1 {
            wrapped.clear();
            wrapped.extend(wrap_usage_parts(&opt_parts, &indent, None, width));
            wrapped.extend(wrap_usage_parts(&pos_parts, &indent, None, width));
        }
        lines.push(prog.to_string());
        lines.extend(wrapped);
    }
    format!("{prefix}{}\n\n", lines.join("\n"))
}

fn usage_tokens(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        while chars.get(index).is_some_and(|ch| ch.is_whitespace()) {
            index += 1;
        }
        if index == chars.len() {
            break;
        }
        let start = index;
        if chars[index] == '(' || chars[index] == '[' {
            let close = if chars[index] == '(' { ')' } else { ']' };
            index += 1;
            let mut closes = 0usize;
            while index < chars.len() {
                if chars[index] == close {
                    closes += 1;
                    index += 1;
                    if closes >= 1 && chars.get(index).is_none_or(|next| next.is_whitespace()) {
                        break;
                    }
                } else {
                    index += 1;
                }
            }
        } else {
            while index < chars.len() && !chars[index].is_whitespace() {
                index += 1;
            }
        }
        tokens.push(chars[start..index].iter().collect());
    }
    tokens
}

fn wrap_usage_parts(
    parts: &[String],
    indent: &str,
    prefix: Option<&str>,
    width: usize,
) -> Vec<String> {
    if parts.is_empty() {
        return Vec::new();
    }
    let indent_len = indent.chars().count();
    let mut result = Vec::new();
    let mut line_parts = Vec::<String>::new();
    let mut line_len = prefix.map_or(indent_len.saturating_sub(1), |value| {
        value.chars().count().saturating_sub(1)
    });
    for part in parts {
        if !line_parts.is_empty() && line_len + 1 + part.chars().count() > width {
            result.push(format!("{indent}{}", line_parts.join(" ")));
            line_parts.clear();
            line_len = indent_len.saturating_sub(1);
        }
        line_len += part.chars().count() + 1;
        line_parts.push(part.clone());
    }
    if !line_parts.is_empty() {
        result.push(format!("{indent}{}", line_parts.join(" ")));
    }
    if prefix.is_some() {
        result[0] = result[0].chars().skip(indent_len).collect();
    }
    result
}

pub fn format_usage(spec: &ParserSpec, width: usize) -> String {
    let actions = display_actions(spec);
    let optionals = usage_parts(spec, &actions, true);
    let mut positionals = usage_parts(spec, &actions, false);
    if let Some(sub) = &spec.sub {
        let metavar = sub.metavar.unwrap_or(sub.dest);
        let part = format!("{metavar} ...");
        positionals.push(if sub.required {
            part
        } else {
            format!("[{part}]")
        });
    }
    wrapped_usage("usage: ", &spec.prog, &optionals, &positionals, width)
}
