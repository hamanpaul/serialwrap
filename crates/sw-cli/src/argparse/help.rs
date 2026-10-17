//! CPython `HelpFormatter`／`RawTextHelpFormatter` 的文字輸出。

use super::{
    action_metavar, display_actions, ActionKind, ActionSpec, Nargs, ParserSpec, SubparsersSpec,
};

fn is_value_action(action: &ActionSpec) -> bool {
    !matches!(
        action.kind,
        ActionKind::Help | ActionKind::Version | ActionKind::StoreTrue | ActionKind::Count
    )
}

fn action_invocation(action: &ActionSpec) -> String {
    if action.option_strings.is_empty() {
        return action_metavar(action);
    }
    if is_value_action(action) {
        let args = match action.nargs {
            Nargs::One => action_metavar(action),
            Nargs::ZeroOrMore => format!("[{} ...]", action_metavar(action)),
        };
        action
            .option_strings
            .iter()
            .map(|option| format!("{option} {args}"))
            .collect::<Vec<_>>()
            .join(", ")
    } else {
        action.option_strings.join(", ")
    }
}

fn subparser_metavar(sub: &SubparsersSpec) -> String {
    sub.metavar.unwrap_or(sub.dest).to_string()
}

fn subparser_action_max(sub: &SubparsersSpec) -> usize {
    let own = subparser_metavar(sub).chars().count();
    sub.entries
        .iter()
        .map(|entry| entry.name.chars().count() + 2)
        .fold(own, usize::max)
}

fn action_max_length(actions: &[ActionSpec], sub: Option<&SubparsersSpec>) -> usize {
    let own = actions
        .iter()
        .map(|action| action_invocation(action).chars().count() + 2)
        .max()
        .unwrap_or(0);
    sub.map_or(own, |sub| own.max(subparser_action_max(sub)))
}

fn action_lines(
    action: &ActionSpec,
    indent: usize,
    help_position: usize,
    width: usize,
    raw_text: bool,
) -> String {
    let invocation = action_invocation(action);
    let help_text = action.help.as_deref().filter(|text| !text.is_empty());
    let action_width = help_position.saturating_sub(indent + 2);
    let (mut header, first_help_indent) = match help_text {
        None => (format!("{}{invocation}\n", " ".repeat(indent)), 0),
        Some(_) if invocation.chars().count() <= action_width => {
            let padding = action_width - invocation.chars().count();
            (
                format!(
                    "{}{invocation}{}  ",
                    " ".repeat(indent),
                    " ".repeat(padding)
                ),
                0,
            )
        }
        Some(_) => (
            format!("{}{invocation}\n", " ".repeat(indent)),
            help_position,
        ),
    };

    if let Some(help_text) = help_text {
        let lines = if raw_text {
            help_text.lines().map(str::to_string).collect::<Vec<_>>()
        } else {
            wrap_help_text(help_text, width.saturating_sub(help_position).max(11))
        };
        if let Some(first) = lines.first() {
            header.push_str(&" ".repeat(first_help_indent));
            header.push_str(first);
            header.push('\n');
            for line in lines.iter().skip(1) {
                header.push_str(&" ".repeat(help_position));
                header.push_str(line);
                header.push('\n');
            }
        } else if !header.ends_with('\n') {
            header.push('\n');
        }
    }
    header
}

fn collapse_ascii_whitespace(text: &str) -> String {
    text.split_ascii_whitespace().collect::<Vec<_>>().join(" ")
}

fn textwrap_whitespace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

fn word_character(ch: char) -> bool {
    ch == '_' || ch.is_alphanumeric()
}

fn letter_like(ch: char) -> bool {
    word_character(ch) && sw_proto::pycompat::py_int_parse(&ch.to_string()).is_none()
}

fn em_dash_word_prefix(ch: char) -> bool {
    word_character(ch) || matches!(ch, '!' | '"' | '\'' | '&' | '.' | ',' | '?')
}

fn split_text_chunks(text: &str) -> Vec<String> {
    let chars: Vec<char> = text
        .chars()
        .map(|ch| if textwrap_whitespace(ch) { ' ' } else { ch })
        .collect();
    let mut chunks = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == ' ' {
            let start = index;
            while chars.get(index) == Some(&' ') {
                index += 1;
            }
            chunks.push(chars[start..index].iter().collect());
            continue;
        }

        let start = index;
        let mut split = false;
        while index < chars.len() && chars[index] != ' ' {
            if chars[index] == '-' {
                let mut dash_end = index + 1;
                while chars.get(dash_end) == Some(&'-') {
                    dash_end += 1;
                }
                let dash_count = dash_end - index;
                if dash_count >= 2
                    && index > start
                    && index > 0
                    && em_dash_word_prefix(chars[index - 1])
                    && dash_end < chars.len()
                    && word_character(chars[dash_end])
                {
                    chunks.push(chars[start..index].iter().collect());
                    chunks.push(chars[index..dash_end].iter().collect());
                    index = dash_end;
                    split = true;
                    break;
                }

                let before_ok =
                    index >= 2 && letter_like(chars[index - 2]) && letter_like(chars[index - 1]);
                let after_ok = chars.get(index + 1).is_some_and(|ch| letter_like(*ch))
                    && (chars.get(index + 2).is_some_and(|ch| letter_like(*ch))
                        || (chars.get(index + 2) == Some(&'-')
                            && chars.get(index + 3).is_some_and(|ch| letter_like(*ch))));
                let doubled_before_ok = index >= 3
                    && letter_like(chars[index - 3])
                    && chars[index - 2] == '-'
                    && letter_like(chars[index - 1]);
                if index + 1 < chars.len()
                    && index > start
                    && ((before_ok && after_ok) || (doubled_before_ok && after_ok))
                {
                    chunks.push(chars[start..=index].iter().collect());
                    index += 1;
                    split = true;
                    break;
                }
                index = dash_end;
                continue;
            }
            index += 1;
        }
        if !split {
            chunks.push(chars[start..index].iter().collect());
        }
    }
    chunks
}

fn take_chars(text: &str, count: usize) -> (String, String) {
    let mut chars = text.chars();
    let head: String = chars.by_ref().take(count).collect();
    (head, chars.collect())
}

fn wrap_help_text(text: &str, width: usize) -> Vec<String> {
    let normalized = collapse_ascii_whitespace(text);
    if normalized.is_empty() {
        return Vec::new();
    }
    let width = width.max(1);
    let mut chunks = split_text_chunks(&normalized);
    chunks.reverse();
    let mut lines = Vec::new();

    while !chunks.is_empty() {
        let mut line = Vec::<String>::new();
        let mut line_len = 0usize;
        if !lines.is_empty() && chunks.last().is_some_and(|chunk| chunk.trim().is_empty()) {
            chunks.pop();
        }
        while let Some(chunk) = chunks.last() {
            let chunk_len = chunk.chars().count();
            if line_len + chunk_len > width {
                break;
            }
            line_len += chunk_len;
            line.push(chunks.pop().expect("檢查過的文字區塊必須存在"));
        }

        if chunks
            .last()
            .is_some_and(|chunk| chunk.chars().count() > width)
        {
            let space_left = width.saturating_sub(line_len);
            if space_left == 0 {
                // CPython 會把空切片加到行尾；只移除此空區塊，保留前一個空白區塊。
                line.push(String::new());
            } else {
                let chunk = chunks.last_mut().expect("檢查過的長區塊必須存在");
                let mut end = space_left;
                if chunk.chars().count() > space_left {
                    let head: String = chunk.chars().take(space_left).collect();
                    if let Some(hyphen) = head.rfind('-') {
                        if hyphen > 0 && head[..hyphen].chars().any(|ch| ch != '-') {
                            end = head[..=hyphen].chars().count();
                        }
                    }
                }
                let (head, tail) = take_chars(chunk, end);
                line.push(head);
                if tail.is_empty() {
                    chunks.pop();
                } else {
                    *chunk = tail;
                }
            }
        }

        if line.last().is_some_and(|chunk| chunk.trim().is_empty()) {
            line.pop();
        }
        if !line.is_empty() {
            lines.push(line.concat());
        }
    }
    lines
}

fn format_text(text: &str, width: usize, raw_text: bool) -> String {
    if raw_text {
        return text
            .split_inclusive('\n')
            .map(|line| format!("{}", line))
            .collect::<String>();
    }
    let text = collapse_ascii_whitespace(text);
    wrap_help_text(&text, width.max(11)).join("\n")
}

fn render_subparser(
    sub: &SubparsersSpec,
    help_position: usize,
    width: usize,
    raw_text: bool,
) -> String {
    let mut out = render_subparser_label(sub);
    for entry in &sub.entries {
        let pseudo = SubparserEntryAction {
            name: entry.name,
            help: entry.help,
        };
        out.push_str(&pseudo.lines(help_position, width, raw_text));
    }
    out
}

fn render_subparser_label(sub: &SubparsersSpec) -> String {
    let invocation = subparser_metavar(sub);
    format!("  {invocation}\n")
}

struct SubparserEntryAction<'a> {
    name: &'a str,
    help: Option<&'a str>,
}

impl SubparserEntryAction<'_> {
    fn lines(&self, help_position: usize, width: usize, raw_text: bool) -> String {
        let invocation = self.name.to_string();
        let help = self.help.map(str::to_string);
        let action_width = help_position.saturating_sub(6);
        let mut out = match help.as_deref().filter(|text| !text.is_empty()) {
            None => format!("    {invocation}\n"),
            Some(_) if invocation.chars().count() <= action_width => {
                let padding = action_width - invocation.chars().count();
                format!("    {invocation}{}  ", " ".repeat(padding))
            }
            Some(_) => format!("    {invocation}\n{}", " ".repeat(help_position)),
        };
        if let Some(help) = help.as_deref().filter(|text| !text.is_empty()) {
            let lines = if raw_text {
                help.lines().map(str::to_string).collect::<Vec<_>>()
            } else {
                wrap_help_text(help, width.saturating_sub(help_position).max(11))
            };
            if let Some(first) = lines.first() {
                out.push_str(first);
                out.push('\n');
                for line in lines.iter().skip(1) {
                    out.push_str(&" ".repeat(help_position));
                    out.push_str(line);
                    out.push('\n');
                }
            }
        }
        out
    }
}

pub fn format_help(spec: &ParserSpec, width: usize) -> String {
    let actions = display_actions(spec);
    let optionals: Vec<&ActionSpec> = actions
        .iter()
        .filter(|action| !action.option_strings.is_empty())
        .collect();
    let positionals: Vec<&ActionSpec> = actions
        .iter()
        .filter(|action| action.option_strings.is_empty())
        .collect();
    let custom_sub = spec.sub.as_ref().filter(|sub| sub.title.is_some());
    let positional_sub = spec.sub.as_ref().filter(|sub| sub.title.is_none());

    let max_length = action_max_length(&actions, spec.sub.as_ref());
    let max_help_position = width.saturating_sub(20).max(4).min(24);
    let help_position = (max_length + 2).min(max_help_position);
    let mut pieces = Vec::new();
    pieces.push(
        super::usage::format_usage(spec, width)
            .trim_end()
            .to_string(),
    );
    if let Some(description) = spec.description {
        let rendered = format_text(description, width, spec.raw_text);
        if !rendered.is_empty() {
            pieces.push(rendered);
        }
    }

    if !positionals.is_empty() || positional_sub.is_some() {
        let mut body = String::new();
        for action in positionals {
            body.push_str(&action_lines(
                action,
                2,
                help_position,
                width,
                spec.raw_text,
            ));
        }
        if let Some(sub) = positional_sub {
            body.push_str(&render_subparser(sub, help_position, width, spec.raw_text));
        }
        pieces.push(format!(
            "{}:\n{}",
            "positional arguments",
            body.trim_end_matches('\n')
        ));
    }

    if !optionals.is_empty() {
        let body = optionals
            .iter()
            .map(|action| action_lines(action, 2, help_position, width, spec.raw_text))
            .collect::<String>();
        pieces.push(format!("options:\n{}", body.trim_end_matches('\n')));
    }

    if let Some(sub) = custom_sub {
        let title = sub.title.unwrap_or("positional arguments");
        let body = render_subparser(sub, help_position, width, spec.raw_text);
        pieces.push(format!("{title}:\n{}", body.trim_end_matches('\n')));
    }

    if let Some(epilog) = spec.epilog {
        let rendered = format_text(epilog, width, spec.raw_text);
        if !rendered.is_empty() {
            pieces.push(rendered);
        }
    }

    format!("{}\n", pieces.join("\n\n"))
}
