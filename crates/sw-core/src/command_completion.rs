//! Rust agent line／background 命令的 echo 錨定完成判定（i294-i1）。
//!
//! producer completion、passthrough、human 命令與其他 op 保留既有路徑。此 evaluator 只把同一份 RX snapshot
//! 中的完整 command echo 作為 prompt 搜尋下界；prompt 身分與無換行輸出裁切由 i294-i2 處理。

use std::collections::HashMap;
use std::ops::Range;

use sw_proto::pycompat::{clean_text, clean_text_with_source_ranges};
use sw_proto::pyre::PyRegex;

use crate::op_runner::{match_wrapped_echo_ending_at, WrapEvidence, WRAP_MIN_SEGMENT};

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
        let (cleaned, source_ranges) = clean_text_with_source_ranges(raw);
        let wraps = WrapEvidence::from_raw(raw, &cleaned, &source_ranges);

        if !raw_half_line {
            let mut metrics = EchoSearchMetrics::default();
            if let Some(echo) =
                find_command_echo(&cleaned, self.command, self.prompt, &wraps, &mut metrics)
            {
                let Some(prompt) = tail_prompt_range(&cleaned, self.prompt)
                    .filter(|prompt| prompt.start >= echo.after_line)
                else {
                    return Evaluation::Pending;
                };
                let body = char_slice(&cleaned, echo.after_line..prompt.start);
                return Evaluation::Complete {
                    stdout: strip_repeated_echo_compat(&body, self.command),
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

/// 保留既有終端連續回顯相容性：只有完整第一行再次等於命令，且後面仍有輸出時才移除。
/// 不以命令後綴推測，也不會剝除唯一一行恰好等於命令的 stdout。
fn strip_repeated_echo_compat(body: &str, command: &str) -> String {
    let body = body.trim_matches('\n');
    let Some((first, rest)) = body.split_once('\n') else {
        return body.to_string();
    };
    if first == command && !rest.trim_matches('\n').is_empty() {
        rest.trim_matches('\n').to_string()
    } else {
        body.to_string()
    }
}

struct EchoRange {
    /// command 字元範圍（不含 prompt 前綴）。
    _command: Range<usize>,
    /// command echo 結束換行後的第一個字元。
    after_line: usize,
}

#[derive(Debug, Default)]
struct EchoSearchMetrics {
    candidate_checks: usize,
    prompt_prefix_checks: usize,
    prefix_scan_chars: usize,
    matcher_steps: usize,
}

fn find_command_echo(
    cleaned: &str,
    command: &str,
    prompt: &PyRegex,
    wraps: &WrapEvidence,
    metrics: &mut EchoSearchMetrics,
) -> Option<EchoRange> {
    let command_chars: Vec<char> = command.chars().collect();
    let chars: Vec<char> = cleaned.chars().collect();
    if command_chars.is_empty() {
        return None;
    }
    let mut line_start_by_char = vec![0usize; chars.len() + 1];
    let mut current_line_start = 0usize;
    for (index, ch) in chars.iter().enumerate() {
        line_start_by_char[index] = current_line_start;
        if *ch == '\n' {
            current_line_start = index + 1;
        }
    }
    line_start_by_char[chars.len()] = current_line_start;
    let mut line_start = 0usize;

    while line_start < chars.len() {
        let line_end = (line_start..chars.len())
            .find(|index| chars[*index] == '\n')
            .unwrap_or(chars.len());
        if line_end < chars.len() {
            metrics.candidate_checks = metrics.candidate_checks.saturating_add(1);
            let mut prompt_cache = HashMap::new();
            let mut valid_start =
                |command_start: usize, first_wrap_command_chars: Option<usize>| {
                    if command_start > line_end {
                        return false;
                    }
                    let command_line_start = line_start_by_char[command_start];
                    let prefix_chars = command_start.saturating_sub(command_line_start);
                    if first_wrap_command_chars.is_some_and(|wrapped_chars| {
                        prefix_chars.saturating_add(wrapped_chars) < WRAP_MIN_SEGMENT
                    }) {
                        return false;
                    }
                    if prefix_chars == 0 {
                        if command_start > 0 && wraps.is_wrap_at(command_start - 1, '\n') {
                            let previous_line_start = line_start_by_char[command_start - 1];
                            if previous_line_start == command_start - 1 {
                                return false;
                            }
                        }
                        return true;
                    }
                    if let Some(matches) = prompt_cache.get(&command_start) {
                        return *matches;
                    }
                    metrics.prompt_prefix_checks = metrics.prompt_prefix_checks.saturating_add(1);
                    metrics.prefix_scan_chars =
                        metrics.prefix_scan_chars.saturating_add(prefix_chars);
                    let prefix: String = chars[command_line_start..command_start].iter().collect();
                    let matches = prompt
                        .search(&prefix)
                        .is_some_and(|matched| matched.start == 0 && matched.end == prefix_chars);
                    prompt_cache.insert(command_start, matches);
                    matches
                };

            let mut direct_steps = 0usize;
            let direct_start = line_end.checked_sub(command_chars.len()).filter(|start| {
                chars[*start..line_end]
                    .iter()
                    .zip(&command_chars)
                    .all(|(actual, expected)| {
                        direct_steps = direct_steps.saturating_add(1);
                        actual == expected
                    })
            });
            metrics.matcher_steps = metrics.matcher_steps.saturating_add(direct_steps);
            if let Some(command_start) = direct_start.filter(|start| valid_start(*start, None)) {
                return Some(EchoRange {
                    _command: command_start..line_end,
                    after_line: line_end + 1,
                });
            }

            let wrap_floor = line_end.saturating_sub(command_chars.len().saturating_mul(2));
            if (wrap_floor..line_end).any(|index| wraps.is_wrap_at(index, chars[index])) {
                let mut matcher_steps = 0usize;
                let candidate = match_wrapped_echo_ending_at(
                    &chars,
                    line_end,
                    &command_chars,
                    wraps,
                    &mut valid_start,
                    &mut matcher_steps,
                );
                metrics.matcher_steps = metrics.matcher_steps.saturating_add(matcher_steps);
                if let Some(command_start) = candidate {
                    return Some(EchoRange {
                        _command: command_start..line_end,
                        after_line: line_end + 1,
                    });
                }
            }
        }
        if line_end == chars.len() {
            break;
        }
        line_start = line_end + 1;
    }
    None
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
        for (raw, marker) in [
            ("noiseX\x08\x1b[31m\r\r\n", "\r\r\n"),
            ("noiseX\x08\x1b[32m \r", " \r"),
        ] {
            let (cleaned, sources) = clean_text_with_source_ranges(raw);
            assert_eq!(cleaned, clean_text(raw));
            let position = cleaned.chars().count() - 1;
            let source = &sources[position];
            let marker_range = if marker == "\r\r\n" {
                source.start - 1..source.end
            } else {
                source.start..source.end + 1
            };
            assert_eq!(&raw[marker_range], marker);
        }

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

        let prpl = evaluator("echo x", r"(?m)^root@prplOS:.*# ");
        for raw in [
            "echo x\r\nfirst\r\nroot@prplOS:~# \r\nmore",
            "echo x\r\nfirst\r\nroot@prplOS:~# extra",
        ] {
            assert_eq!(
                prpl.evaluate(raw, false, false),
                Evaluation::Pending,
                "prefix prompt 不完整或不在 snapshot 尾端時不得完成：{raw:?}"
            );
        }

        let unanchored = evaluator("echo x", r"root@t:.*# ");
        assert_eq!(
            unanchored.evaluate("echo x\r\nfirst root@t:/# ", false, false),
            Evaluation::Pending,
            "未錨定的 prompt regex 不得在最後一行中間命中"
        );
    }

    #[test]
    fn echo_boundary_preserves_command_like_output_lines() {
        let exact = evaluator("cat /tmp/job", r"(?m)^root@t:.*# ");
        assert_eq!(
            exact.evaluate(
                "cat /tmp/job\r\ncat /tmp/job\r\nroot@t:/# ",
                false,
                false,
            ),
            Evaluation::Complete {
                stdout: "cat /tmp/job".into()
            }
        );

        let suffix = evaluator("cat /tmp/job", r"(?m)^root@t:.*# ");
        assert_eq!(
            suffix.evaluate(
                "cat /tmp/job\r\ndiagnostic: cat /tmp/job\r\nnext\r\nroot@t:/# ",
                false,
                false,
            ),
            Evaluation::Complete {
                stdout: "diagnostic: cat /tmp/job\nnext".into()
            }
        );

        let repeated = evaluator("ls", r"(?m)^root@t:.*# ");
        assert_eq!(
            repeated.evaluate("ls\r\nls\r\na b\r\nroot@t:/# ", false, false),
            Evaluation::Complete { stdout: "a b".into() }
        );

        // i1-R2-01 編排者裁決：這是保留的窄範圍相容行為，即使第一行也可能是合法 stdout。
        let documented_compatibility = evaluator("cat /tmp/job", r"(?m)^root@t:.*# ");
        assert_eq!(
            documented_compatibility.evaluate(
                "cat /tmp/job\r\ncat /tmp/job\r\nnext\r\nroot@t:/# ",
                false,
                false,
            ),
            Evaluation::Complete { stdout: "next".into() }
        );
    }

    #[test]
    fn wrapped_echo_matching_handles_whitespace_ambiguities() {
        let prompt = r"(?m)^root@t:.*# ";
        let check = |command: &str, echo: &str, expected: Evaluation| {
            let evaluator = evaluator(command, prompt);
            let raw = format!("{echo}\r\nOUT\r\nroot@t:/# ");
            assert_eq!(evaluator.evaluate(&raw, true, false), expected, "{raw:?}");
        };
        let complete = Evaluation::Complete {
            stdout: "OUT".into(),
        };
        let uncertain = |stdout: String| Evaluation::Uncertain { stdout };

        // 命令原有空白正好位於 BusyBox 折行前；原始 ` \r\r\n` 同時符合空白與換行標記。
        let command = format!("echo {} {}", "x".repeat(40), "y".repeat(40));
        let cut = 5 + 40 + 1;
        let echo = format!("{}\r\r\n{}", &command[..cut], &command[cut..]);
        check(&command, &echo, complete.clone());

        // readline 插入空白分別位於命令空白之前、之後。
        let command = format!("echo {} {}", "x".repeat(74), "y".repeat(20));
        let before = 5 + 74;
        for cut in [before, before + 1] {
            let echo = format!("{} \r{}", &command[..cut], &command[cut..]);
            check(&command, &echo, complete.clone());
        }

        // 連續兩個命令空白之間有 readline 插入空白。
        let command = format!("echo {}  {}", "x".repeat(40), "y".repeat(40));
        let cut = 5 + 40 + 1;
        let echo = format!("{} \r{}", &command[..cut], &command[cut..]);
        check(&command, &echo, complete.clone());

        // 同一 echo 混用 BusyBox 與 readline 標記，且有多次折行。
        let command = format!("echo {} {} {}", "a".repeat(30), "b".repeat(35), "c".repeat(40));
        let echo = format!(
            "{}\r\r\n{} \r{}",
            &command[..25],
            &command[25..60],
            &command[60..]
        );
        check(&command, &echo, complete.clone());

        // 命令第一個字元之前的 readline 標記不是 echo 內折行；最後字元之後的插入空白也不能完成 echo。
        let command = format!("echo {}", "z".repeat(40));
        let leading = format!(" \r{command}");
        check(&command, &leading, uncertain(format!(" {command}\nOUT")));
        let leading_busybox = format!("\r\r\n{command}");
        check(&command, &leading_busybox, uncertain(format!("{command}\nOUT")));
        let trailing_readline = format!("{command} \r");
        check(
            &command,
            &trailing_readline,
            uncertain(format!("{command} \nOUT")),
        );

        // BusyBox 換行在命令最後一個字元之後是完整 echo 的行界線。
        let trailing_busybox = format!("{command}\r\r\n");
        check(&command, &trailing_busybox, complete);
    }

    #[test]
    fn wrap_evidence_uses_the_surviving_source_after_backspace() {
        let raw = "A \r \x08";
        let (cleaned, sources) = clean_text_with_source_ranges(raw);
        assert_eq!(cleaned, "A ");
        assert_eq!(sources, vec![0..1, 1..2]);
        let evidence = WrapEvidence::from_raw(raw, &cleaned, &sources);
        assert!(evidence.is_wrap_at(1, ' '));

        let raw = "A \r\x08 ";
        let (cleaned, sources) = clean_text_with_source_ranges(raw);
        assert_eq!(cleaned, "A ");
        assert_eq!(sources, vec![0..1, 4..5]);
        let evidence = WrapEvidence::from_raw(raw, &cleaned, &sources);
        assert!(!evidence.is_wrap_at(1, ' '));
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

    #[test]
    fn i294_long_rx_echo_search_is_bounded() {
        let evaluator = evaluator("echo x", r"(?m)^root@t:.*# ");
        let long_line = format!("{}\nroot@t:/# ", "e".repeat(100_000));
        let (cleaned, source_ranges) = clean_text_with_source_ranges(&long_line);
        let wraps = WrapEvidence::from_raw(&long_line, &cleaned, &source_ranges);
        let input_chars = cleaned.chars().count();
        let line_count = cleaned.chars().filter(|ch| *ch == '\n').count() + 1;
        let mut metrics = EchoSearchMetrics::default();
        let started = std::time::Instant::now();
        let _ = find_command_echo(
            &cleaned,
            "echo x",
            evaluator.prompt,
            &wraps,
            &mut metrics,
        );
        assert!(
            metrics.candidate_checks <= line_count * 2,
            "每行至多檢查行首與 prompt 後起點：{metrics:?}"
        );
        assert!(
            metrics.prompt_prefix_checks <= line_count,
            "prompt 前綴每行只檢查一次：{metrics:?}"
        );
        assert!(
            metrics.prefix_scan_chars <= input_chars,
            "prompt 前綴總掃描字元數不得超過 RX 輸入長度：{metrics:?}"
        );
        assert!(
            metrics.matcher_steps <= metrics.candidate_checks * "echo x".chars().count(),
            "無折行時 matcher 工作量須受候選數 × 命令長度限制：{metrics:?}"
        );

        assert_eq!(
            evaluator.evaluate(&long_line, false, false),
            Evaluation::Complete {
                stdout: "e".repeat(100_000)
            }
        );

        let short_lines = (0..2_000)
            .map(|index| format!("e{index}\n"))
            .collect::<String>()
            + "root@t:/# ";
        assert_eq!(
            evaluator.evaluate(&short_lines, false, false),
            Evaluation::Complete {
                stdout: short_lines.trim_end_matches("root@t:/# ").trim_end_matches('\n').to_string()
            }
        );
        let candidates_then_echo = format!(
            "{}root@t:/# echo x\r\nx\r\nroot@t:/# ",
            "e\n".repeat(2_000)
        );
        let (cleaned, source_ranges) = clean_text_with_source_ranges(&candidates_then_echo);
        let wraps = WrapEvidence::from_raw(&candidates_then_echo, &cleaned, &source_ranges);
        let mut late_echo_metrics = EchoSearchMetrics::default();
        let line_count = cleaned.chars().filter(|ch| *ch == '\n').count() + 1;
        let echo = find_command_echo(
            &cleaned,
            "echo x",
            evaluator.prompt,
            &wraps,
            &mut late_echo_metrics,
        );
        assert!(echo.is_some(), "大量前置候選後仍須找到行尾直接 echo");
        assert!(
            late_echo_metrics.candidate_checks <= line_count,
            "每個行尾至多檢查一次：{late_echo_metrics:?}"
        );
        assert!(
            late_echo_metrics.prompt_prefix_checks <= line_count,
            "每個候選起點的 prompt 前綴只掃描一次：{late_echo_metrics:?}"
        );
        assert_eq!(
            evaluator.evaluate(&candidates_then_echo, false, false),
            Evaluation::Complete { stdout: "x".into() }
        );
        assert_eq!(
            evaluator.evaluate("human residue\nroot@t:/# ", true, false),
            Evaluation::Uncertain {
                stdout: "human residue".into()
            }
        );
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "長 RX 完成判定超過寬鬆上限：{:?}",
            started.elapsed()
        );
    }

    #[test]
    fn i294_end_anchored_prompt_prefix_keeps_echo_stdout_boundary() {
        let evaluator = evaluator("echo x", r"(?m)^root@t:/# $");
        assert_eq!(
            evaluator.evaluate("root@t:/# echo x\r\nx\r\nroot@t:/# ", false, false),
            Evaluation::Complete { stdout: "x".into() }
        );
    }

}
