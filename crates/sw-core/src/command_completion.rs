//! Rust agent line／background 命令的 echo 錨定完成判定（i294-i1）。
//!
//! producer completion、passthrough、human 命令與其他 op 保留既有路徑。此 evaluator 只把同一份 RX snapshot
//! 中的完整 command echo 作為 prompt 搜尋下界；prompt 身分與無換行輸出裁切由 i294-i2 處理。

use std::ops::Range;

use sw_proto::pycompat::clean_text;
use sw_proto::pyre::PyRegex;

use crate::op_runner::{match_wrapped_echo, strip_command_echo_from_text, WrapEvidence};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Evaluation {
    Pending,
    Complete { stdout: String },
    Uncertain { stdout: String },
}

/// 以 snapshot 本身計算 echo、prompt 與 stdout 邊界，命中後不再讀取或搜尋較新的 RX。
pub(crate) struct CompletionEvaluator<'a> {
    command: &'a str,
    prompt: &'a PyRegex,
}

impl<'a> CompletionEvaluator<'a> {
    pub(crate) fn new(command: &'a str, prompt: &'a PyRegex) -> Self {
        Self { command, prompt }
    }

    pub(crate) fn has_tail_prompt(&self, raw: &str) -> bool {
        tail_prompt_range(&clean_text(raw), self.prompt).is_some()
    }

    pub(crate) fn evaluate(
        &self,
        raw: &str,
        human_tx_seen: bool,
        raw_half_line: bool,
    ) -> Evaluation {
        let cleaned = clean_text(raw);
        let wraps = WrapEvidence {
            newline: raw.contains("\r\r\n"),
            space: raw.contains(" \r"),
        };

        if !raw_half_line {
            if let Some(echo) = find_command_echo(&cleaned, self.command, self.prompt, wraps) {
                let Some(prompt) = self.prompt.search_from(&cleaned, echo.after_line) else {
                    return Evaluation::Pending;
                };
                let body = char_slice(&cleaned, echo.after_line..prompt.start);
                return Evaluation::Complete {
                    stdout: strip_command_echo_from_text(raw, &body, self.command),
                };
            }
        }

        if human_tx_seen {
            let stdout = tail_prompt_range(&cleaned, self.prompt)
                .map(|prompt| char_slice(&cleaned, 0..prompt.start).trim_matches('\n').to_string())
                .unwrap_or_else(|| cleaned.clone());
            return Evaluation::Uncertain { stdout };
        }

        match tail_prompt_range(&cleaned, self.prompt) {
            Some(prompt) => Evaluation::Complete {
                stdout: char_slice(&cleaned, 0..prompt.start).trim_matches('\n').to_string(),
            },
            None => Evaluation::Pending,
        }
    }
}

struct EchoRange {
    /// command 字元範圍（不含 prompt 前綴）。
    _command: Range<usize>,
    /// command echo 結束換行後的第一個字元。
    after_line: usize,
}

fn find_command_echo(
    cleaned: &str,
    command: &str,
    prompt: &PyRegex,
    wraps: WrapEvidence,
) -> Option<EchoRange> {
    let command_chars: Vec<char> = command.chars().collect();
    let chars: Vec<char> = cleaned.chars().collect();
    let first = *command_chars.first()?;
    let mut line_start = 0usize;

    while line_start < chars.len() {
        let line_end = chars[line_start..]
            .iter()
            .position(|c| *c == '\n')
            .map_or(chars.len(), |n| line_start + n);
        for candidate in line_start..line_end {
            if chars[candidate] != first {
                continue;
            }
            let prefix = char_slice(cleaned, line_start..candidate);
            if !prefix.is_empty() && !is_complete_prompt_prefix(&prefix, prompt) {
                continue;
            }
            let local_start = candidate - line_start;
            let Some(local_end) = match_wrapped_echo(
                &chars[line_start..],
                local_start,
                &command_chars,
                wraps,
            ) else {
                continue;
            };
            let command_end = line_start + local_end;
            if chars.get(command_end) == Some(&'\n') {
                return Some(EchoRange {
                    _command: candidate..command_end,
                    after_line: command_end + 1,
                });
            }
        }
        if line_end == chars.len() {
            break;
        }
        line_start = line_end + 1;
    }
    None
}

fn is_complete_prompt_prefix(prefix: &str, prompt: &PyRegex) -> bool {
    prompt
        .search(prefix)
        .is_some_and(|matched| matched.start == 0 && matched.end == prefix.chars().count())
}

/// 無 echo 的相容降階只接受 snapshot 尾端、最後一行行首的完整 prompt match。
fn tail_prompt_range(cleaned: &str, prompt: &PyRegex) -> Option<Range<usize>> {
    let len = cleaned.chars().count();
    let line_start = cleaned
        .chars()
        .enumerate()
        .filter_map(|(index, ch)| (ch == '\n').then_some(index + 1))
        .last()
        .unwrap_or(0);
    let tail = char_slice(cleaned, line_start..len);
    let matched = prompt.search(&tail)?;
    (matched.start == 0 && matched.end == tail.chars().count())
        .then_some(line_start..len)
}

fn char_slice(text: &str, range: Range<usize>) -> String {
    text.chars()
        .skip(range.start)
        .take(range.end.saturating_sub(range.start))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use sw_proto::pyre::{compile, PyFlags};

    fn evaluator<'a>(command: &'a str, prompt: &'static str) -> CompletionEvaluator<'a> {
        let prompt = Box::leak(Box::new(compile(prompt, PyFlags(0)).expect("prompt regex")));
        CompletionEvaluator::new(command, prompt)
    }

    #[test]
    fn complete_echo_and_prefix_prompt_share_snapshot() {
        let prpl = evaluator("echo x", r"(?m)^root@prplOS:.*# ");
        assert_eq!(
            prpl.evaluate("old human\nroot@prplOS:~# echo x\r\nx\r\nroot@prplOS:~# ", false, false),
            Evaluation::Complete { stdout: "x".into() }
        );

        let speedport = evaluator(
            "echo x",
            r"(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n#]*#[ \t]*",
        );
        assert_eq!(
            speedport.evaluate(
                "stale human\nroot@Speedport Smart 4 Typ A:~# echo x\r\nx\r\nroot@Speedport Smart 4 Typ A:~# ",
                false,
                false,
            ),
            Evaluation::Complete { stdout: "x".into() }
        );
    }

    #[test]
    fn no_echo_human_tx_is_uncertain_but_clean_no_echo_tail_prompt_completes() {
        let evaluator = evaluator("printf x", r"(?m)^root@prplOS:.*# ");
        assert_eq!(
            evaluator.evaluate("x\nroot@prplOS:~# ", true, false),
            Evaluation::Uncertain { stdout: "x".into() }
        );
        assert_eq!(
            evaluator.evaluate("x\nroot@prplOS:~# ", false, false),
            Evaluation::Complete { stdout: "x".into() }
        );
    }

    #[test]
    fn raw_half_line_is_not_accepted_as_clean_echo() {
        let evaluator = evaluator("echo x", r"(?m)^root@t:.*# ");
        assert_eq!(
            evaluator.evaluate("root@t:/# echo x\r\nx\r\nroot@t:/# ", true, true),
            Evaluation::Uncertain { stdout: "root@t:/# echo x\nx".into() }
        );
    }
}
