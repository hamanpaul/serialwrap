//! CPython argparse 錯誤文字與 exit 2。

use super::{py_repr, ActionSpec, ExitRequest, ParserSpec};

pub(super) fn action_name(action: &ActionSpec) -> String {
    if action.option_strings.is_empty() {
        action.dest.to_string()
    } else {
        action.option_strings.join("/")
    }
}

pub(super) fn argument_error(action: &ActionSpec, message: &str) -> String {
    format!("argument {}: {message}", action_name(action))
}

pub(super) fn invalid_value(action: &ActionSpec, kind: &str, value: &str) -> String {
    argument_error(action, &format!("invalid {kind} value: {}", py_repr(value)))
}

pub(super) fn invalid_choice(action: &ActionSpec, value: &str) -> String {
    let choices = action
        .choices
        .as_ref()
        .map(|items| {
            items
                .iter()
                .map(|item| py_repr(item))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    argument_error(
        action,
        &format!("invalid choice: {} (choose from {choices})", py_repr(value)),
    )
}

pub(super) fn missing_required(names: &[String]) -> String {
    format!("the following arguments are required: {}", names.join(", "))
}

pub(super) fn required_mutex(names: &[String]) -> String {
    format!("one of the arguments {} is required", names.join(" "))
}

pub(super) fn unrecognized(arguments: &[String]) -> String {
    format!("unrecognized arguments: {}", arguments.join(" "))
}

pub(super) fn ambiguous(option: &str, matches: &[String]) -> String {
    format!(
        "ambiguous option: {option} could match {}",
        matches.join(", ")
    )
}

pub(super) fn exit_error(spec: &ParserSpec, width: usize, message: &str) -> ExitRequest {
    let usage = super::usage::format_usage(spec, width);
    let usage = usage.trim_end_matches('\n');
    ExitRequest {
        code: 2,
        stdout: String::new(),
        stderr: format!("{usage}\n{}: error: {message}\n", spec.prog),
    }
}
