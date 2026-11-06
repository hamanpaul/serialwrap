//! CPython 3.12.3 `_parse_known_args` 的 CLI 子集。

use super::{
    display_actions, ActionKind, ActionSpec, ArgValue, Conv, ExitRequest, Namespace, Nargs,
    ParseResult, ParserSpec, SubTarget,
};
use crate::argparse::{error, help};
use std::collections::{BTreeMap, HashSet};

enum ParseFailure {
    Exit(ExitRequest),
    Delegated,
}

#[derive(Clone, Debug)]
struct OptionTuple {
    action: Option<usize>,
    option: String,
    separator: Option<String>,
    explicit: Option<String>,
}

enum OptionParse {
    Positional,
    Optional(OptionTuple),
    Ambiguous { token: String, matches: Vec<String> },
}

#[derive(Clone, Copy)]
enum PositionalAction {
    Action(usize),
    Subparser,
}

struct ConsumePositionals {
    end: usize,
    subparser_seen: bool,
    child_extras: Vec<String>,
}

fn is_no_value(action: &ActionSpec) -> bool {
    matches!(
        action.kind,
        ActionKind::Help | ActionKind::Version | ActionKind::StoreTrue | ActionKind::Count
    )
}

fn is_py_decimal(ch: char) -> bool {
    sw_proto::pycompat::py_int_parse(&ch.to_string()).is_some()
}

/// 對應 CPython argparse 的 `^-\d+$|^-\d*\.\d+$`。
fn negative_number(token: &str) -> bool {
    let Some(value) = token.strip_prefix('-') else {
        return false;
    };
    if !value.is_empty() && value.chars().all(is_py_decimal) {
        return true;
    }
    let Some((whole, fraction)) = value.split_once('.') else {
        return false;
    };
    !fraction.is_empty()
        && !fraction.contains('.')
        && whole.chars().all(is_py_decimal)
        && fraction.chars().all(is_py_decimal)
}

fn has_negative_number_optionals(actions: &[ActionSpec]) -> bool {
    actions
        .iter()
        .flat_map(|action| action.option_strings.iter())
        .any(|option| negative_number(option))
}

fn get_option_tuples(token: &str, actions: &[ActionSpec], allow_abbrev: bool) -> Vec<OptionTuple> {
    let chars: Vec<char> = token.chars().collect();
    if chars.len() < 2 || chars[0] != '-' {
        return Vec::new();
    }

    let mut result = Vec::new();
    if chars[1] == '-' {
        if !allow_abbrev {
            return result;
        }
        let (prefix, separator, explicit) = match token.split_once('=') {
            Some((prefix, value)) => (prefix, Some("=".to_string()), Some(value.to_string())),
            None => (token, None, None),
        };
        for (index, action) in actions.iter().enumerate() {
            for option in &action.option_strings {
                if option.starts_with(prefix) {
                    result.push(OptionTuple {
                        action: Some(index),
                        option: (*option).to_string(),
                        separator: separator.clone(),
                        explicit: explicit.clone(),
                    });
                }
            }
        }
        return result;
    }

    let short_prefix: String = chars.iter().take(2).collect();
    let explicit: String = chars.iter().skip(2).collect();
    for (index, action) in actions.iter().enumerate() {
        for option in &action.option_strings {
            if *option == short_prefix {
                result.push(OptionTuple {
                    action: Some(index),
                    option: (*option).to_string(),
                    separator: Some(String::new()),
                    explicit: Some(explicit.clone()),
                });
            } else if option.starts_with(token) {
                result.push(OptionTuple {
                    action: Some(index),
                    option: (*option).to_string(),
                    separator: None,
                    explicit: None,
                });
            }
        }
    }
    result
}

fn parse_optional(token: &str, actions: &[ActionSpec], allow_abbrev: bool) -> OptionParse {
    if token.is_empty() || !token.starts_with('-') {
        return OptionParse::Positional;
    }
    if let Some((index, action)) = actions
        .iter()
        .enumerate()
        .find(|(_, action)| action.option_strings.iter().any(|option| *option == token))
    {
        return OptionParse::Optional(OptionTuple {
            action: Some(index),
            option: token.to_string(),
            separator: None,
            explicit: None,
        });
    }
    if token.chars().count() == 1 {
        return OptionParse::Positional;
    }

    if let Some((option, explicit)) = token.split_once('=') {
        if let Some((index, _)) = actions.iter().enumerate().find(|(_, action)| {
            action
                .option_strings
                .iter()
                .any(|candidate| *candidate == option)
        }) {
            return OptionParse::Optional(OptionTuple {
                action: Some(index),
                option: option.to_string(),
                separator: Some("=".to_string()),
                explicit: Some(explicit.to_string()),
            });
        }
    }

    let candidates = get_option_tuples(token, actions, allow_abbrev);
    if candidates.len() > 1 {
        return OptionParse::Ambiguous {
            token: token.to_string(),
            matches: candidates
                .into_iter()
                .map(|candidate| candidate.option)
                .collect(),
        };
    }
    if let Some(candidate) = candidates.into_iter().next() {
        return OptionParse::Optional(candidate);
    }

    if negative_number(token) && !has_negative_number_optionals(actions) {
        return OptionParse::Positional;
    }
    if token.contains(' ') {
        return OptionParse::Positional;
    }
    OptionParse::Optional(OptionTuple {
        action: None,
        option: token.to_string(),
        separator: None,
        explicit: None,
    })
}

fn classify_args(
    args: &[String],
    actions: &[ActionSpec],
    allow_abbrev: bool,
    spec: &ParserSpec,
    width: usize,
) -> Result<(Vec<char>, BTreeMap<usize, OptionTuple>), ParseFailure> {
    let mut pattern = Vec::with_capacity(args.len());
    let mut option_indices = BTreeMap::new();
    let mut after_separator = false;
    for (index, token) in args.iter().enumerate() {
        if after_separator {
            pattern.push('A');
        } else if token == "--" {
            pattern.push('-');
            after_separator = true;
        } else {
            match parse_optional(token, actions, allow_abbrev) {
                OptionParse::Positional => pattern.push('A'),
                OptionParse::Optional(option) => {
                    pattern.push('O');
                    option_indices.insert(index, option);
                }
                OptionParse::Ambiguous { token, matches } => {
                    let message = error::ambiguous(&token, &matches);
                    return Err(ParseFailure::Exit(error::exit_error(spec, width, &message)));
                }
            }
        }
    }
    Ok((pattern, option_indices))
}

fn action_value(
    action: &ActionSpec,
    values: &[String],
    positional: bool,
) -> Result<ArgValue, String> {
    let mut values = values.to_vec();
    if positional {
        if let Some(index) = values.iter().position(|value| value == "--") {
            values.remove(index);
        }
    }
    if action.nargs == Nargs::ZeroOrMore {
        if positional && values.is_empty() && action.default != ArgValue::None {
            return Ok(action.default.clone());
        }
        if action.conv == Conv::Str {
            for value in &values {
                if let Some(choices) = &action.choices {
                    if !choices.contains(&value.as_str()) {
                        return Err(error::invalid_choice(action, value));
                    }
                }
            }
        }
        return Ok(ArgValue::List(values));
    }
    let Some(value) = values.first() else {
        return Ok(action.default.clone());
    };
    let converted = match action.conv {
        Conv::Str => ArgValue::Str(value.clone()),
        Conv::Int => match sw_proto::pycompat::py_int_parse(value) {
            Some(value) => ArgValue::Int(value),
            None => return Err(error::invalid_value(action, "int", value)),
        },
        Conv::Float => match sw_proto::pycompat::py_float_parse(value) {
            Some(value) => ArgValue::Float(value),
            None => return Err(error::invalid_value(action, "float", value)),
        },
    };
    if let (ArgValue::Str(value), Some(choices)) = (&converted, &action.choices) {
        if !choices.contains(&value.as_str()) {
            return Err(error::invalid_choice(action, value));
        }
    }
    Ok(converted)
}

fn record_action(
    action: &ActionSpec,
    index: usize,
    value: ArgValue,
    non_default: bool,
    actions: &[ActionSpec],
    namespace: &mut Namespace,
    seen_actions: &mut HashSet<usize>,
    seen_non_default: &mut HashSet<usize>,
    spec: &ParserSpec,
    width: usize,
) -> Result<(), ParseFailure> {
    for group in &spec.mutex {
        if !group.dests.contains(&action.dest) {
            continue;
        }
        if let Some(previous) = group
            .dests
            .iter()
            .filter_map(|dest| actions.iter().position(|candidate| candidate.dest == *dest))
            .find(|previous| *previous != index && seen_non_default.contains(previous))
        {
            let message = error::argument_error(
                action,
                &format!(
                    "not allowed with argument {}",
                    error::action_name(&actions[previous])
                ),
            );
            return Err(ParseFailure::Exit(error::exit_error(spec, width, &message)));
        }
    }
    namespace.vals.insert(action.dest.to_string(), value);
    seen_actions.insert(index);
    if non_default {
        seen_non_default.insert(index);
    }
    Ok(())
}

fn apply_action(
    index: usize,
    action: &ActionSpec,
    raw_values: &[String],
    positional: bool,
    actions: &[ActionSpec],
    namespace: &mut Namespace,
    seen_actions: &mut HashSet<usize>,
    seen_non_default: &mut HashSet<usize>,
    spec: &ParserSpec,
    width: usize,
) -> Result<(), ParseFailure> {
    let value = match action.kind {
        ActionKind::StoreTrue => ArgValue::Bool(true),
        ActionKind::Count => {
            let current = match namespace.get(action.dest) {
                ArgValue::Count(value) => *value,
                ArgValue::Int(sw_proto::pyjson::PyInt::Small(value)) => *value as i64,
                _ => 0,
            };
            ArgValue::Count(current + 1)
        }
        ActionKind::Append => {
            let parsed = action_value(action, raw_values, positional)
                .map_err(|message| ParseFailure::Exit(error::exit_error(spec, width, &message)))?;
            let ArgValue::Str(item) = parsed else {
                return Err(ParseFailure::Exit(error::exit_error(
                    spec,
                    width,
                    "append action requires a string value",
                )));
            };
            let mut values = match namespace.get(action.dest) {
                ArgValue::List(values) => values.clone(),
                _ => Vec::new(),
            };
            values.push(item);
            ArgValue::List(values)
        }
        ActionKind::Store => action_value(action, raw_values, positional)
            .map_err(|message| ParseFailure::Exit(error::exit_error(spec, width, &message)))?,
        ActionKind::Help | ActionKind::Version => return Ok(()),
    };
    let mut positional_values = raw_values.to_vec();
    if positional {
        if let Some(index) = positional_values.iter().position(|value| value == "--") {
            positional_values.remove(index);
        }
    }
    let non_default = !(positional
        && action.nargs == Nargs::ZeroOrMore
        && positional_values.is_empty()
        && action.default != ArgValue::None);
    record_action(
        action,
        index,
        value,
        non_default,
        actions,
        namespace,
        seen_actions,
        seen_non_default,
        spec,
        width,
    )
}

fn apply_option_tuples(
    tuples: Vec<(usize, Vec<String>)>,
    actions: &[ActionSpec],
    namespace: &mut Namespace,
    seen_actions: &mut HashSet<usize>,
    seen_non_default: &mut HashSet<usize>,
    spec: &ParserSpec,
    width: usize,
) -> Result<(), ParseFailure> {
    for (index, values) in tuples {
        let action = &actions[index];
        if action.kind == ActionKind::Help {
            return Err(ParseFailure::Exit(ExitRequest {
                code: 0,
                stdout: help::format_help(spec, width),
                stderr: String::new(),
            }));
        }
        if action.kind == ActionKind::Version {
            return Err(ParseFailure::Exit(ExitRequest {
                code: 0,
                stdout: format!("{}\n", action.version.as_deref().unwrap_or_default()),
                stderr: String::new(),
            }));
        }
        apply_action(
            index,
            action,
            &values,
            false,
            actions,
            namespace,
            seen_actions,
            seen_non_default,
            spec,
            width,
        )?;
    }
    Ok(())
}

fn ignored_explicit_argument(
    action: &ActionSpec,
    value: &str,
    spec: &ParserSpec,
    width: usize,
) -> ParseFailure {
    let message = error::argument_error(
        action,
        &format!("ignored explicit argument {}", super::py_repr(value)),
    );
    ParseFailure::Exit(error::exit_error(spec, width, &message))
}

fn argument_count(action: &ActionSpec, pattern: &[char]) -> Result<usize, ()> {
    if is_no_value(action) {
        return Ok(0);
    }
    match action.nargs {
        Nargs::One => {
            if pattern.first() == Some(&'A') {
                Ok(1)
            } else {
                Err(())
            }
        }
        Nargs::ZeroOrMore => Ok(pattern.iter().take_while(|&&ch| ch == 'A').count()),
    }
}

fn expected_one_argument(spec: &ParserSpec, width: usize, action: &ActionSpec) -> ParseFailure {
    let message = error::argument_error(action, "expected one argument");
    ParseFailure::Exit(error::exit_error(spec, width, &message))
}

fn consume_optional(
    start: usize,
    args: &[String],
    pattern: &[char],
    option_indices: &BTreeMap<usize, OptionTuple>,
    actions: &[ActionSpec],
    namespace: &mut Namespace,
    seen_actions: &mut HashSet<usize>,
    seen_non_default: &mut HashSet<usize>,
    extras: &mut Vec<String>,
    spec: &ParserSpec,
    width: usize,
) -> Result<usize, ParseFailure> {
    let tuple = option_indices
        .get(&start)
        .cloned()
        .expect("選項索引必須存在");
    let Some(mut action_index) = tuple.action else {
        extras.push(args[start].clone());
        return Ok(start + 1);
    };
    let mut option = tuple.option;
    let mut separator = tuple.separator;
    let mut explicit = tuple.explicit;
    let mut action_tuples = Vec::<(usize, Vec<String>)>::new();
    let mut stop = start + 1;

    loop {
        let action = &actions[action_index];
        if let Some(value) = explicit.take() {
            let count = if is_no_value(action) { 0 } else { 1 };
            if count == 0 && option.chars().nth(1) != Some('-') && !value.is_empty() {
                let separated = separator.as_deref().is_some_and(|sep| !sep.is_empty());
                if separated || value.starts_with('-') {
                    return Err(ignored_explicit_argument(action, &value, spec, width));
                }
                action_tuples.push((action_index, Vec::new()));
                let mut value_chars = value.chars();
                let first = value_chars.next().expect("非空明確值必須有字元");
                let next_option = format!("-{}", first);
                let rest: String = value_chars.collect();
                let Some((next_index, _)) = actions.iter().enumerate().find(|(_, candidate)| {
                    candidate
                        .option_strings
                        .iter()
                        .any(|candidate_option| *candidate_option == next_option)
                }) else {
                    extras.push(format!("-{}", value));
                    break;
                };
                action_index = next_index;
                option = next_option;
                separator = None;
                if rest.is_empty() {
                    explicit = None;
                } else if let Some(rest) = rest.strip_prefix('=') {
                    separator = Some("=".to_string());
                    explicit = Some(rest.to_string());
                } else {
                    separator = Some(String::new());
                    explicit = Some(rest);
                }
                continue;
            }
            if count == 1 {
                action_tuples.push((action_index, vec![value]));
                break;
            }
            return Err(ignored_explicit_argument(action, &value, spec, width));
        }

        let remaining_pattern = pattern.get(start + 1..).unwrap_or_default();
        let count = match argument_count(action, remaining_pattern) {
            Ok(count) => count,
            Err(()) => return Err(expected_one_argument(spec, width, action)),
        };
        let values = args
            .get(start + 1..start + 1 + count)
            .unwrap_or_default()
            .to_vec();
        action_tuples.push((action_index, values));
        stop = start + 1 + count;
        break;
    }

    apply_option_tuples(
        action_tuples,
        actions,
        namespace,
        seen_actions,
        seen_non_default,
        spec,
        width,
    )?;
    Ok(stop)
}

fn action_match_lengths(action: &ActionSpec, pattern: &[char], start: usize) -> Vec<usize> {
    let remaining = &pattern[start..];
    match action.nargs {
        Nargs::One => {
            let mut leading = 0;
            while remaining.get(leading) == Some(&'-') {
                leading += 1;
            }
            if remaining.get(leading) != Some(&'A') {
                return Vec::new();
            }
            let mut end = leading + 1;
            while remaining.get(end) == Some(&'-') {
                end += 1;
            }
            (leading + 1..=end).rev().collect()
        }
        Nargs::ZeroOrMore => {
            let max = remaining
                .iter()
                .take_while(|&&ch| ch == 'A' || ch == '-')
                .count();
            (0..=max).rev().collect()
        }
    }
}

fn positional_action_match_lengths(
    action: PositionalAction,
    actions: &[ActionSpec],
    pattern: &[char],
    start: usize,
) -> Vec<usize> {
    match action {
        PositionalAction::Action(index) => action_match_lengths(&actions[index], pattern, start),
        PositionalAction::Subparser => {
            let remaining = &pattern[start..];
            let mut cursor = 0;
            while remaining.get(cursor) == Some(&'-') {
                cursor += 1;
            }
            if remaining.get(cursor) != Some(&'A') {
                return Vec::new();
            }
            cursor += 1;
            while matches!(remaining.get(cursor), Some('A' | 'O' | '-')) {
                cursor += 1;
            }
            (cursor..=cursor).rev().collect()
        }
    }
}

fn find_positional_match(
    positionals: &[PositionalAction],
    actions: &[ActionSpec],
    pattern: &[char],
    start: usize,
    action_index: usize,
    counts: &mut Vec<usize>,
) -> bool {
    if action_index == positionals.len() {
        return true;
    }
    for count in positional_action_match_lengths(positionals[action_index], actions, pattern, start)
    {
        if start + count > pattern.len() {
            continue;
        }
        counts.push(count);
        if find_positional_match(
            positionals,
            actions,
            pattern,
            start + count,
            action_index + 1,
            counts,
        ) {
            return true;
        }
        counts.pop();
    }
    false
}

fn match_positionals(
    positionals: &[PositionalAction],
    actions: &[ActionSpec],
    pattern: &[char],
    start: usize,
) -> Vec<usize> {
    for count in (1..=positionals.len()).rev() {
        let mut counts = Vec::with_capacity(count);
        if find_positional_match(
            &positionals[..count],
            actions,
            pattern,
            start,
            0,
            &mut counts,
        ) {
            return counts;
        }
    }
    Vec::new()
}

fn consume_positionals(
    start: usize,
    args: &[String],
    pattern: &[char],
    actions: &[ActionSpec],
    positionals: &mut Vec<PositionalAction>,
    namespace: &mut Namespace,
    seen_actions: &mut HashSet<usize>,
    seen_non_default: &mut HashSet<usize>,
    spec: &ParserSpec,
    width: usize,
) -> Result<ConsumePositionals, ParseFailure> {
    let counts = match_positionals(positionals, actions, pattern, start);
    let mut offset = start;
    let mut subparser_seen = false;
    let mut child_extras = Vec::new();
    for (action, count) in positionals.iter().copied().zip(counts.iter().copied()) {
        let values = args
            .get(offset..offset + count)
            .unwrap_or_default()
            .to_vec();
        offset += count;
        match action {
            PositionalAction::Action(index) => apply_action(
                index,
                &actions[index],
                &values,
                true,
                actions,
                namespace,
                seen_actions,
                seen_non_default,
                spec,
                width,
            )?,
            PositionalAction::Subparser => {
                let Some(sub) = &spec.sub else {
                    continue;
                };
                let Some(name) = values.first() else {
                    continue;
                };
                let Some(entry) = sub.entries.iter().find(|entry| entry.name == name) else {
                    let choices = sub
                        .entries
                        .iter()
                        .map(|entry| super::py_repr(entry.name))
                        .collect::<Vec<_>>()
                        .join(", ");
                    let metavar = sub.metavar.unwrap_or(sub.dest);
                    let message = format!(
                        "argument {metavar}: invalid choice: {} (choose from {choices})",
                        super::py_repr(name)
                    );
                    return Err(ParseFailure::Exit(error::exit_error(spec, width, &message)));
                };
                namespace
                    .vals
                    .insert(sub.dest.to_string(), ArgValue::Str(name.clone()));
                subparser_seen = true;
                match &entry.target {
                    SubTarget::Delegated => return Err(ParseFailure::Delegated),
                    SubTarget::Native(child) => {
                        let child_args = &values[1..];
                        child_extras = parse_level(child, child_args, width, namespace)?;
                    }
                }
            }
        }
    }
    positionals.drain(..counts.len());
    Ok(ConsumePositionals {
        end: offset,
        subparser_seen,
        child_extras,
    })
}

fn parse_level(
    spec: &ParserSpec,
    args: &[String],
    width: usize,
    namespace: &mut Namespace,
) -> Result<Vec<String>, ParseFailure> {
    let actions = display_actions(spec);
    for action in &actions {
        if matches!(action.kind, ActionKind::Help | ActionKind::Version) {
            continue;
        }
        namespace
            .vals
            .entry(action.dest.to_string())
            .or_insert_with(|| action.default.clone());
    }
    if let Some(sub) = &spec.sub {
        namespace
            .vals
            .entry(sub.dest.to_string())
            .or_insert(ArgValue::None);
    }

    let mut positionals: Vec<PositionalAction> = actions
        .iter()
        .enumerate()
        .filter(|(_, action)| action.option_strings.is_empty())
        .map(|(index, _)| PositionalAction::Action(index))
        .collect();
    if spec.sub.is_some() {
        positionals.push(PositionalAction::Subparser);
    }
    let (pattern, option_indices) = classify_args(args, &actions, spec.allow_abbrev, spec, width)?;
    let mut seen_actions = HashSet::<usize>::new();
    let mut seen_non_default = HashSet::<usize>::new();
    let mut extras = Vec::<String>::new();
    let mut subparser_seen = false;
    let mut child_extras = Vec::<String>::new();
    let max_option_index = option_indices.keys().next_back().copied();
    let mut start = 0usize;

    if let Some(max_option_index) = max_option_index {
        while start <= max_option_index {
            let Some(next_option_index) = option_indices.range(start..).next().map(|(&i, _)| i)
            else {
                break;
            };
            if start != next_option_index {
                let previous_start = start;
                let consumed = consume_positionals(
                    start,
                    args,
                    &pattern,
                    &actions,
                    &mut positionals,
                    namespace,
                    &mut seen_actions,
                    &mut seen_non_default,
                    spec,
                    width,
                )?;
                start = consumed.end;
                subparser_seen |= consumed.subparser_seen;
                child_extras.extend(consumed.child_extras);
                if start > previous_start {
                    continue;
                }
            }
            if !option_indices.contains_key(&start) {
                extras.extend_from_slice(&args[start..next_option_index]);
                start = next_option_index;
            }
            start = consume_optional(
                start,
                args,
                &pattern,
                &option_indices,
                &actions,
                namespace,
                &mut seen_actions,
                &mut seen_non_default,
                &mut extras,
                spec,
                width,
            )?;
        }
    }

    let consumed = consume_positionals(
        start,
        args,
        &pattern,
        &actions,
        &mut positionals,
        namespace,
        &mut seen_actions,
        &mut seen_non_default,
        spec,
        width,
    )?;
    let stop_index = consumed.end;
    subparser_seen |= consumed.subparser_seen;
    child_extras.extend(consumed.child_extras);
    extras.extend_from_slice(&args[stop_index..]);

    let mut missing = Vec::<String>::new();
    for (index, action) in actions.iter().enumerate() {
        if matches!(action.kind, ActionKind::Help | ActionKind::Version) {
            continue;
        }
        let positional_required = action.option_strings.is_empty() && action.nargs == Nargs::One;
        if (action.required || positional_required) && !seen_actions.contains(&index) {
            missing.push(error::action_name(action));
        }
    }
    if let Some(sub) = &spec.sub {
        if sub.required && !subparser_seen {
            missing.push(sub.metavar.unwrap_or(sub.dest).to_string());
        }
    }
    if !missing.is_empty() {
        return Err(ParseFailure::Exit(error::exit_error(
            spec,
            width,
            &error::missing_required(&missing),
        )));
    }

    for group in &spec.mutex {
        if group.required
            && !group.dests.iter().any(|dest| {
                actions
                    .iter()
                    .position(|action| action.dest == *dest)
                    .is_some_and(|index| seen_non_default.contains(&index))
            })
        {
            let names = group
                .dests
                .iter()
                .filter_map(|dest| actions.iter().find(|action| action.dest == *dest))
                .map(error::action_name)
                .collect::<Vec<_>>();
            return Err(ParseFailure::Exit(error::exit_error(
                spec,
                width,
                &error::required_mutex(&names),
            )));
        }
    }
    extras.extend(child_extras);
    Ok(extras)
}

pub fn parse_args(spec: &ParserSpec, args: &[String], width: usize) -> ParseResult {
    let mut namespace = Namespace::default();
    match parse_level(spec, args, width, &mut namespace) {
        Ok(extras) if !extras.is_empty() => ParseResult::Exit(error::exit_error(
            spec,
            width,
            &error::unrecognized(&extras),
        )),
        Ok(_) => ParseResult::Ok(namespace),
        Err(ParseFailure::Exit(exit)) => ParseResult::Exit(exit),
        Err(ParseFailure::Delegated) => ParseResult::Delegated,
    }
}
