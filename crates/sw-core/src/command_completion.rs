//! Rust agent line／background 命令的 echo 與 prompt 身分完成判定（i294）。
//!
//! producer completion、passthrough、human 命令與其他 op 保留既有路徑。此 evaluator 只把同一份 RX snapshot
//! 中的完整 command echo 與 epoch 內 prompt 字面證據作為完成邊界，並以同一邊界裁切無換行 stdout。

use std::ops::Range;

use sw_proto::pycompat::{clean_text, clean_text_with_source_ranges};
use sw_proto::pyre::PyRegex;

use crate::op_runner::{
    match_wrapped_echo_ending_at, WrapEvidence, WrappedEchoCandidate, WrappedEchoMatchMetrics,
    WRAP_MIN_SEGMENT,
};
use crate::uart_bridge::prompt_evidence_literal_is_identifiable;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Evaluation {
    Pending,
    Complete { stdout: String },
    Partial { stdout: String },
    Uncertain { stdout: String },
}

#[derive(Clone, Copy)]
pub(crate) struct CompletionHumanContext {
    tx_seen: bool,
    cross_tx_prefix_blocked: bool,
}

impl CompletionHumanContext {
    pub(crate) fn new(
        tx_seen: bool,
        human_in_flight: usize,
        tx_submit_pending: bool,
    ) -> Self {
        Self {
            tx_seen,
            cross_tx_prefix_blocked: tx_seen
                || human_in_flight > 0
                || tx_submit_pending,
        }
    }
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

    pub(crate) fn evaluate_snapshot(
        &self,
        raw: &str,
        human: CompletionHumanContext,
        raw_half_line: bool,
        prompt_evidence: Option<&str>,
        trimmed_before_from: bool,
        line_prefix_before_from: Option<&str>,
    ) -> (Evaluation, Option<String>) {
        let (cleaned, source_ranges) = clean_text_with_source_ranges(raw);
        let wraps = WrapEvidence::from_raw(raw, &cleaned, &source_ranges);
        let raw_echo = find_command_echo(
            &cleaned,
            self.command,
            self.prompt,
            &wraps,
            &mut EchoSearchMetrics::default(),
        );
        let mut echo = raw_echo;
        let mut echo_cleaned = cleaned.clone();
        let mut may_observe_prefix = echo
            .as_ref()
            .is_some_and(|echo| echo.line_start > 0);
        if echo.as_ref().is_some_and(|echo| echo.line_start == 0) {
            if let Some(prefix) = line_prefix_before_from {
                let echo_raw = format!("{prefix}{raw}");
                let (combined_cleaned, combined_ranges) =
                    clean_text_with_source_ranges(&echo_raw);
                let combined_wraps =
                    WrapEvidence::from_raw(&echo_raw, &combined_cleaned, &combined_ranges);
                let combined_echo = find_command_echo(
                    &combined_cleaned,
                    self.command,
                    self.prompt,
                    &combined_wraps,
                    &mut EchoSearchMetrics::default(),
                )
                .filter(|echo| echo.line_start == 0);
                if let Some(combined_echo) = combined_echo {
                    echo = Some(combined_echo);
                    echo_cleaned = combined_cleaned;
                    may_observe_prefix = !human.cross_tx_prefix_blocked;
                }
            }
        }

        if !raw_half_line {
            if let Some(echo) = echo {
                let observed_literal = if may_observe_prefix {
                    echo.prompt_evidence
                } else {
                    None
                };
                let observed_prompt =
                    observed_literal.and_then(|literal| validate_echo_prompt_prefix(literal, self.prompt));
                let observed_range =
                    prompt_evidence_line_start_tail_range(
                        &echo_cleaned,
                        observed_prompt.as_deref(),
                    )
                        .filter(|prompt| prompt.start >= echo.after_line);
                let existing_range = prompt_evidence_tail_range(&echo_cleaned, prompt_evidence)
                    .filter(|prompt| prompt.start >= echo.after_line);
                let short_line_prompt = (observed_prompt.is_none()).then(|| {
                    regex_tail_prompt_range(&echo_cleaned, self.prompt).filter(|prompt| {
                        prompt_is_symbol_only(&char_slice(&echo_cleaned, prompt.clone()))
                    })
                });
                let Some(prompt) = existing_range
                    .clone()
                    .or(observed_range.clone())
                    .or(short_line_prompt.flatten())
                else {
                    let has_candidate =
                        regex_tail_prompt_range(&echo_cleaned, self.prompt).is_some()
                        || short_prompt_suffix_range(&echo_cleaned, self.prompt).is_some()
                        || prompt_evidence_suffix_range(
                            &echo_cleaned,
                            observed_prompt.as_deref(),
                        )
                            .is_some()
                        || prompt_evidence_suffix_range(
                            &echo_cleaned,
                            prompt_evidence,
                        )
                        .is_some();
                    if !has_candidate {
                        return (Evaluation::Pending, None);
                    }
                    let stdout = char_slice(
                        &echo_cleaned,
                        echo.after_line..echo_cleaned.chars().count(),
                    )
                            .trim_matches('\n')
                            .to_string();
                    return (Evaluation::Uncertain { stdout }, None);
                };
                let body = char_slice(&echo_cleaned, echo.after_line..prompt.start);
                let learned_prompt = observed_range.and_then(|_| {
                    observed_prompt.filter(|literal| prompt_evidence != Some(literal.as_str()))
                });
                return (
                    Evaluation::Complete {
                        stdout: strip_repeated_echo_compat(&body, self.command),
                    },
                    learned_prompt,
                );
            }
        }

        if human.tx_seen {
            let stdout = prompt_evidence_tail_range(&cleaned, prompt_evidence)
                .map(|prompt| char_slice(&cleaned, 0..prompt.start).trim_matches('\n').to_string())
                .unwrap_or_else(|| cleaned.clone());
            return (Evaluation::Uncertain { stdout }, None);
        }

        let regex_prompt = regex_tail_prompt_range(&cleaned, self.prompt);
        match regex_prompt {
            Some(prompt) if trimmed_before_from => (
                Evaluation::Partial {
                    stdout: char_slice(&cleaned, 0..prompt.start)
                        .trim_matches('\n')
                        .to_string(),
                },
                None,
            ),
            Some(prompt) => (
                Evaluation::Complete {
                    stdout: char_slice(&cleaned, 0..prompt.start)
                        .trim_matches('\n')
                        .to_string(),
                },
                None,
            ),
            None if regex_tail_prompt_range(&cleaned, self.prompt).is_some()
                || short_prompt_suffix_range(&cleaned, self.prompt).is_some()
                || prompt_evidence_suffix_range(&cleaned, prompt_evidence).is_some() =>
            (
                Evaluation::Uncertain {
                    stdout: cleaned.trim_matches('\n').to_string(),
                },
                None,
            ),
            None => (Evaluation::Pending, None),
        }
    }

    /// 從逾時 snapshot 留下可用輸出；此回傳值只用於 partial，不得將 timeout 改判為 Complete。
    pub(crate) fn partial_stdout(
        &self,
        raw: &str,
        human: CompletionHumanContext,
        raw_half_line: bool,
        prompt_evidence: Option<&str>,
        line_prefix_before_from: Option<&str>,
    ) -> String {
        let (evaluation, _) = self.evaluate_snapshot(
            raw,
            human,
            raw_half_line,
            prompt_evidence,
            false,
            line_prefix_before_from,
        );
        match evaluation {
            Evaluation::Complete { stdout }
            | Evaluation::Partial { stdout }
            | Evaluation::Uncertain { stdout } => stdout,
            Evaluation::Pending => clean_text(raw).trim_matches('\n').to_string(),
        }
    }

    #[cfg(test)]
    fn evaluate(&self, raw: &str, human_tx_seen: bool, raw_half_line: bool) -> Evaluation {
        let (cleaned, _) = clean_text_with_source_ranges(raw);
        let evidence = regex_tail_prompt_range(&cleaned, self.prompt)
            .map(|prompt| char_slice(&cleaned, prompt));
        self.evaluate_snapshot(
            raw,
            CompletionHumanContext::new(human_tx_seen, 0, false),
            raw_half_line,
            evidence.as_deref(),
            false,
            Some(""),
        )
        .0
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
    /// 清理後 command echo 所在行的真實行首（在 echo snapshot 座標系）。
    line_start: usize,
    /// command 字元範圍（不含 prompt 前綴）。
    _command: Range<usize>,
    /// command echo 結束換行後的第一個字元。
    after_line: usize,
    /// 已確認完整 echo 前方、完整匹配 profile regex 的 prompt 字面。
    prompt_evidence: Option<String>,
}

#[derive(Debug, Default)]
struct EchoSearchMetrics {
    candidate_checks: usize,
    prompt_prefix_checks: usize,
    prefix_scan_chars: usize,
    preprocess_steps: usize,
    lcp_index_slots: usize,
    matcher_steps: usize,
    wrap_range_queries: usize,
    ambiguous_wrap_windows: usize,
    max_wrap_marker_slots: usize,
    max_candidate_len: usize,
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
    let reversed_command: Vec<char> = command_chars.iter().rev().copied().collect();
    let reversed_chars: Vec<char> = chars.iter().rev().copied().collect();
    metrics.preprocess_steps = metrics
        .preprocess_steps
        .saturating_add(command_chars.len())
        .saturating_add(chars.len());
    let prefix_lcp = lcp_lengths(&command_chars, &chars, &mut metrics.preprocess_steps);
    let suffix_lcp = lcp_lengths(&reversed_command, &reversed_chars, &mut metrics.preprocess_steps);
    metrics.lcp_index_slots = prefix_lcp.len().saturating_add(suffix_lcp.len());
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
                    metrics.prompt_prefix_checks = metrics.prompt_prefix_checks.saturating_add(1);
                    metrics.prefix_scan_chars =
                        metrics.prefix_scan_chars.saturating_add(prefix_chars);
                    let prefix: String = chars[command_line_start..command_start].iter().collect();
                    prompt
                        .search(&prefix)
                        .is_some_and(|matched| matched.start == 0 && matched.end == prefix_chars)
                };

            let direct_start = line_end.checked_sub(command_chars.len()).filter(|start| {
                metrics.matcher_steps = metrics.matcher_steps.saturating_add(1);
                prefix_lcp[*start] >= command_chars.len()
            });
            if let Some(command_start) = direct_start.filter(|start| valid_start(*start, None)) {
                return Some(EchoRange {
                    line_start: line_start_by_char[command_start],
                    _command: command_start..line_end,
                    after_line: line_end + 1,
                    prompt_evidence: echo_prompt_prefix(
                        &chars,
                        line_start_by_char[command_start],
                        command_start,
                    ),
                });
            }

            let candidate_len = command_chars.len().saturating_add(1);
            let wrap_start = line_end.saturating_sub(candidate_len);
            metrics.wrap_range_queries = metrics.wrap_range_queries.saturating_add(1);
            let wrap_count = wraps.wrap_count(wrap_start, line_end);
            if wrap_count > 1 {
                metrics.ambiguous_wrap_windows =
                    metrics.ambiguous_wrap_windows.saturating_add(1);
            } else if let Some(marker_position) = wraps.sole_wrap_in(wrap_start, line_end) {
                let mut matcher_metrics = WrappedEchoMatchMetrics::default();
                metrics.max_candidate_len = metrics.max_candidate_len.max(candidate_len);
                let candidate = match_wrapped_echo_ending_at(
                    WrappedEchoCandidate {
                        text: &chars,
                        end: line_end,
                        command: &command_chars,
                        marker_position,
                        prefix_lcp: &prefix_lcp,
                        suffix_lcp: &suffix_lcp,
                    },
                    &mut valid_start,
                    &mut matcher_metrics,
                );
                metrics.matcher_steps = metrics
                    .matcher_steps
                    .saturating_add(matcher_metrics.matcher_steps);
                metrics.max_wrap_marker_slots = metrics
                    .max_wrap_marker_slots
                    .max(matcher_metrics.max_marker_slots);
                if let Some(command_start) = candidate {
                    return Some(EchoRange {
                        line_start: line_start_by_char[command_start],
                        _command: command_start..line_end,
                        after_line: line_end + 1,
                        prompt_evidence: echo_prompt_prefix(
                            &chars,
                            line_start_by_char[command_start],
                            command_start,
                        ),
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

/// 對 pattern 與 text 的每個起點計算最長共同前綴，使用 Z 演算法確保整體 O(pattern + text)。
fn lcp_lengths(pattern: &[char], text: &[char], operations: &mut usize) -> Vec<usize> {
    let mut sequence = Vec::with_capacity(pattern.len() + 1 + text.len());
    sequence.extend(pattern.iter().copied().map(Some));
    sequence.push(None);
    sequence.extend(text.iter().copied().map(Some));
    *operations = (*operations).saturating_add(sequence.len());

    let mut z = vec![0usize; sequence.len()];
    let (mut left, mut right) = (0usize, 0usize);
    for index in 1..sequence.len() {
        *operations = (*operations).saturating_add(1);
        if index < right {
            z[index] = z[index - left].min(right - index);
        }
        while index + z[index] < sequence.len() {
            *operations = (*operations).saturating_add(1);
            if sequence[z[index]] != sequence[index + z[index]] {
                break;
            }
            z[index] += 1;
        }
        if index + z[index] > right {
            left = index;
            right = index + z[index];
        }
    }

    let text_start = pattern.len() + 1;
    *operations = (*operations).saturating_add(text.len());
    z[text_start..].to_vec()
}

fn echo_prompt_prefix(chars: &[char], line_start: usize, command_start: usize) -> Option<String> {
    let literal: String = chars
        .get(line_start..command_start)?
        .iter()
        .collect();
    prompt_evidence_literal_is_identifiable(&literal).then_some(literal)
}

fn validate_echo_prompt_prefix(
    literal: String,
    prompt: &PyRegex,
) -> Option<String> {
    let matched = prompt.search(&literal);
    (prompt_evidence_literal_is_identifiable(&literal)
        && matched.is_some_and(|matched| {
            matched.start == 0 && matched.end == literal.chars().count()
        }))
    .then_some(literal)
}

fn prompt_evidence_tail_range(cleaned: &str, evidence: Option<&str>) -> Option<Range<usize>> {
    let range = prompt_evidence_suffix_range(cleaned, evidence)?;
    let chars = cleaned.chars().collect::<Vec<_>>();
    let line_start = chars[..range.start]
        .iter()
        .rposition(|ch| *ch == '\n')
        .map_or(0, |index| index + 1);
    if range.start != line_start
        && !chars[range.clone()]
            .iter()
            .any(|ch| ch.is_alphanumeric())
    {
        return None;
    }
    Some(range)
}

fn prompt_evidence_line_start_tail_range(
    cleaned: &str,
    evidence: Option<&str>,
) -> Option<Range<usize>> {
    let range = prompt_evidence_suffix_range(cleaned, evidence)?;
    let chars = cleaned.chars().collect::<Vec<_>>();
    let line_start = chars[..range.start]
        .iter()
        .rposition(|ch| *ch == '\n')
        .map_or(0, |index| index + 1);
    (range.start == line_start).then_some(range)
}

fn prompt_evidence_suffix_range(cleaned: &str, evidence: Option<&str>) -> Option<Range<usize>> {
    let evidence_chars = evidence?.chars().collect::<Vec<_>>();
    if evidence_chars.is_empty() {
        return None;
    }
    let chars = cleaned.chars().collect::<Vec<_>>();
    let start = chars.len().checked_sub(evidence_chars.len())?;
    if chars.get(start..)? != evidence_chars {
        return None;
    }
    Some(start..chars.len())
}

fn prompt_is_symbol_only(literal: &str) -> bool {
    !literal.is_empty()
        && !literal.contains(['\0', '\r', '\n'])
        && !literal.chars().any(char::is_alphanumeric)
        && literal.chars().any(|ch| !ch.is_whitespace())
}

fn short_prompt_suffix_range(cleaned: &str, prompt: &PyRegex) -> Option<Range<usize>> {
    let chars = cleaned.chars().collect::<Vec<_>>();
    let line_start = chars
        .iter()
        .rposition(|ch| *ch == '\n')
        .map_or(0, |index| index + 1);
    let suffix_start = chars[line_start..]
        .iter()
        .rposition(|ch| ch.is_alphanumeric())
        .map_or(line_start, |index| line_start + index + 1);
    let suffix: String = chars.get(suffix_start..)?.iter().collect();
    if !prompt_is_symbol_only(&suffix) {
        return None;
    }
    let matched = prompt.search(&suffix)?;
    (matched.start == 0 && matched.end == suffix.chars().count())
        .then_some(suffix_start..chars.len())
}

fn regex_tail_prompt_range(cleaned: &str, prompt: &PyRegex) -> Option<Range<usize>> {
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
    use crate::uart_bridge::{RxShared, SnapshotWaitResult};
    use sw_pal::traits::Clock;
    use sw_proto::pyre::{compile, PyFlags};
    use std::time::Duration;

    fn evaluator<'a>(command: &'a str, prompt: &'static str) -> CompletionEvaluator<'a> {
        let prompt = Box::leak(Box::new(compile(prompt, PyFlags(0)).expect("prompt regex")));
        CompletionEvaluator::new(command, prompt)
    }

    #[test]
    fn i294_r8_snapshot_evaluator_that_finishes_after_deadline_is_timed_out() {
        let clock = sw_pal::fakes::clock::FakeClock::new();
        let rx = RxShared::new(4096, clock.mono_ns());
        let raw = "echo DEADLINE\r\nDEADLINE\r\nroot@t:/# ";
        rx.append(raw.as_bytes(), clock.mono_ns(), 1, 1);
        let evaluator = evaluator("echo DEADLINE", r"(?m)^root@t:/# ");
        let deadline = clock.mono_ns().saturating_add(10);
        let before_human = rx.human_tx_state();
        let before_watermark = rx.watermark();

        let result = rx.wait_for_snapshot_until_from(
            0,
            deadline,
            before_human,
            before_watermark,
            &*clock,
            |snapshot| match evaluator.evaluate(&snapshot.text, false, false) {
                Evaluation::Complete { stdout } | Evaluation::Partial { stdout } => {
                    clock.advance(Duration::from_nanos(10));
                    Some(stdout)
                }
                Evaluation::Pending | Evaluation::Uncertain { .. } => None,
            },
        );

        assert!(
            matches!(result, SnapshotWaitResult::TimedOut { .. }),
            "完整證據雖在 deadline snapshot 內，但 evaluator 跨過截止點才返回，必須 timeout"
        );
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

        let arrow = evaluator(
            "cat /tmp/job",
            r"(?m)^(?:[ \t]*|.*[^>#\s])[>#][ \t]*$",
        );
        let (evaluation, learned) = arrow.evaluate_snapshot(
            "Path => cat /tmp/job\r\nB => ",
            CompletionHumanContext::new(false, 0, false),
            false,
            None,
            false,
            Some(""),
        );
        assert_eq!(learned, None, "=> 輸出不能成為 prompt evidence");
        assert_eq!(
            evaluation,
            Evaluation::Uncertain {
                stdout: "B => ".into()
            }
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

        // 預先裁決 1：多次或混用折行無法可靠定位 echo，套用無 echo／human TX 規則。
        let command = format!("echo {} {} {}", "a".repeat(30), "b".repeat(35), "c".repeat(40));
        let echo = format!(
            "{}\r\r\n{} \r{}",
            &command[..25],
            &command[25..60],
            &command[60..]
        );
        let multi_wrap = evaluator(&command, prompt);
        let result = multi_wrap.evaluate(&format!("{echo}\r\nOUT\r\nroot@t:/# "), true, false);
        let Evaluation::Uncertain { stdout } = result else {
            panic!("多次／混用折行必須套用無 echo 降階：{result:?}");
        };
        assert!(stdout.ends_with("OUT"), "無 echo 降階須保留可用輸出：{stdout:?}");

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
    fn i294_r8_ambiguous_fold_search_has_linear_work_bound() {
        let prompt = r"(?m)^root@t:/# ";
        for command_len in [40usize, 100, 200, 400] {
            let command = format!("X{}", " ".repeat(command_len - 1));
            let hostile_line = " \r".repeat(command_len * 2);
            let raw = format!("{hostile_line}\r\nOUT\r\nroot@t:/# ");
            let evaluator = evaluator(&command, prompt);
            let (cleaned, source_ranges) = clean_text_with_source_ranges(&raw);
            let wraps = WrapEvidence::from_raw(&raw, &cleaned, &source_ranges);
            let mut metrics = EchoSearchMetrics::default();
            let _ = find_command_echo(
                &cleaned,
                &command,
                evaluator.prompt,
                &wraps,
                &mut metrics,
            );
            let input_chars = cleaned.chars().count();
            let linear_budget = 16 * (input_chars + command.chars().count());
            let counted_operations = metrics
                .candidate_checks
                .saturating_add(metrics.prefix_scan_chars)
                .saturating_add(metrics.preprocess_steps)
                .saturating_add(metrics.matcher_steps)
                .saturating_add(metrics.wrap_range_queries);
            assert!(
                counted_operations <= linear_budget,
                "行掃描、prompt 前綴、matcher 與折行索引查詢須受輸入與命令長度線性限制：len={command_len}, budget={linear_budget}, operations={counted_operations}, {metrics:?}"
            );
            assert!(
                wraps.index_slots() <= 3 * (input_chars + 1),
                "折行索引配置須為輸入長度線性上界：len={command_len}, input={input_chars}, slots={}, {metrics:?}",
                wraps.index_slots()
            );
            assert!(
                metrics.lcp_index_slots <= 2 * input_chars,
                "LCP 索引空間須為輸入長度線性上界：input={input_chars}, slots={}, {metrics:?}",
                metrics.lcp_index_slots
            );
            assert!(
                metrics.max_wrap_marker_slots <= 1,
                "折行 matcher 只保留一個候選插入點：{metrics:?}"
            );
            assert!(
                metrics.max_candidate_len <= command_len + 1,
                "matcher 暫存候選不得超過命令長度加一：{metrics:?}"
            );
            assert!(
                metrics.ambiguous_wrap_windows > 0,
                "800 個重複 readline 標記中，含多個標記的候選必須降階：{metrics:?}"
            );
            assert!(
                matches!(evaluator.evaluate(&raw, true, false), Evaluation::Uncertain { .. }),
                "大量有局部標記的候選不得被誤認為 echo：len={command_len}"
            );
        }

        let command = format!("X{}", " ".repeat(399));
        let many_short_lines = format!("{}root@t:/# ", "x\n".repeat(2_000));
        let evaluator = evaluator(&command, r"(?m)^root@t:/# ");
        let (cleaned, source_ranges) = clean_text_with_source_ranges(&many_short_lines);
        let wraps = WrapEvidence::from_raw(&many_short_lines, &cleaned, &source_ranges);
        let mut metrics = EchoSearchMetrics::default();
        let _ = find_command_echo(
            &cleaned,
            &command,
            evaluator.prompt,
            &wraps,
            &mut metrics,
        );
        let input_chars = cleaned.chars().count();
        let counted_operations = metrics
            .candidate_checks
            .saturating_add(metrics.prefix_scan_chars)
            .saturating_add(metrics.preprocess_steps)
            .saturating_add(metrics.matcher_steps)
            .saturating_add(metrics.wrap_range_queries);
        assert!(
            counted_operations <= 16 * (input_chars + command.chars().count()),
            "無折行的短行集合仍須線性：operations={counted_operations}, input={input_chars}, {metrics:?}"
        );
        assert!(
            wraps.index_slots() <= 3 * (input_chars + 1),
            "大量短行的索引記憶體須為輸入長度線性：input={input_chars}, slots={}",
            wraps.index_slots()
        );
        assert!(metrics.lcp_index_slots <= 2 * input_chars);
    }

    #[test]
    fn i294_r9_direct_echo_search_has_linear_work_bound() {
        let prompt = r"(?m)^root@t:/# ";
        for command_len in [40usize, 100, 200, 400, 800, 1_600] {
            let command = "a".repeat(command_len);
            let raw = format!("{}b{}", "a".repeat(command_len - 1), "\n".repeat(command_len));
            let evaluator = evaluator(&command, prompt);
            let (cleaned, source_ranges) = clean_text_with_source_ranges(&raw);
            let wraps = WrapEvidence::from_raw(&raw, &cleaned, &source_ranges);
            let mut metrics = EchoSearchMetrics::default();
            let echo = find_command_echo(
                &cleaned,
                &command,
                evaluator.prompt,
                &wraps,
                &mut metrics,
            );
            assert!(echo.is_none(), "不一致的直接比對輸入不得找到 echo：len={command_len}");

            let input_chars = cleaned.chars().count();
            let operation_count = metrics
                .candidate_checks
                .saturating_add(metrics.prompt_prefix_checks)
                .saturating_add(metrics.prefix_scan_chars)
                .saturating_add(metrics.preprocess_steps)
                .saturating_add(metrics.matcher_steps)
                .saturating_add(metrics.wrap_range_queries);
            let linear_budget = 16 * (input_chars + command_len);
            assert!(
                operation_count <= linear_budget,
                "直接 echo 候選比對須有線性操作上界：command_len={command_len}, input={input_chars}, budget={linear_budget}, operations={operation_count}, {metrics:?}"
            );
            assert!(
                metrics.lcp_index_slots <= 2 * input_chars,
                "直接比對索引空間須為輸入長度線性：command_len={command_len}, input={input_chars}, slots={}, {metrics:?}",
                metrics.lcp_index_slots
            );
            assert!(
                wraps.index_slots() <= 3 * (input_chars + 1),
                "來源索引空間須為輸入長度線性：command_len={command_len}, input={input_chars}, slots={}, {metrics:?}",
                wraps.index_slots()
            );
        }
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
