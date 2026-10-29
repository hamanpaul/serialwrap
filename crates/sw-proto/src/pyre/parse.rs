//! CPython 3.12 re 語法解析器與 AST。

use std::collections::HashMap;

use super::{CompileError, PyFlags, PyReError, PARSE_DEPTH_LIMIT};

const FLAG_I: u32 = PyFlags::I;
const FLAG_L: u32 = PyFlags::L;
const FLAG_M: u32 = PyFlags::M;
const FLAG_S: u32 = PyFlags::S;
const FLAG_U: u32 = PyFlags::U;
const FLAG_X: u32 = PyFlags::X;
const FLAG_A: u32 = PyFlags::A;
const FLAG_T: u32 = PyFlags::T;
const KNOWN_FLAGS: u32 = FLAG_T | FLAG_I | FLAG_L | FLAG_M | FLAG_S | FLAG_U | FLAG_X | FLAG_A;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum CategoryKind {
    Digit,
    Space,
    Word,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Category {
    pub kind: CategoryKind,
    pub negated: bool,
    pub ascii: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum ClassItem {
    Character(char),
    Range(char, char),
    Category(Category),
}

/// 字元類別內的一個 token。`raw` 為 CPython tokenizer 的原文（跳脫只取反斜線與其後一個字元），
/// 用於 `bad character range` 訊息。
#[derive(Clone, Debug)]
struct ClassToken {
    item: ClassItem,
    raw: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Anchor {
    Start,
    End { multiline: bool },
    AbsoluteStart,
    AbsoluteEnd,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum RepeatMode {
    Greedy,
    Lazy,
    Possessive,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Expr {
    Empty,
    Literal(char),
    Dot,
    Category(Category),
    CharacterClass {
        negated: bool,
        items: Vec<ClassItem>,
    },
    Anchor(Anchor),
    WordBoundary {
        boundary: bool,
        ascii: bool,
    },
    Sequence(Vec<Expr>),
    Alternation(Vec<Expr>),
    NonCapture(Box<Expr>),
    Capture {
        index: usize,
        body: Box<Expr>,
    },
    Backreference {
        index: usize,
    },
    Conditional {
        group: usize,
        yes: Box<Expr>,
        no: Box<Expr>,
    },
    Repeat {
        body: Box<Expr>,
        min: usize,
        max: Option<usize>,
        mode: RepeatMode,
    },
    LookAhead {
        positive: bool,
        body: Box<Expr>,
    },
    LookBehind {
        positive: bool,
        body: Box<Expr>,
    },
    Atomic(Box<Expr>),
    ScopedFlags {
        add: u32,
        remove: u32,
        body: Box<Expr>,
    },
}

#[derive(Clone, Debug)]
pub(super) struct ParsedPattern {
    pub expression: Expr,
    pub global_flags: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Width {
    min: usize,
    max: Option<usize>,
}

impl Width {
    fn zero() -> Self {
        Self {
            min: 0,
            max: Some(0),
        }
    }

    fn one() -> Self {
        Self {
            min: 1,
            max: Some(1),
        }
    }
}

struct Parser<'a> {
    pattern: &'a str,
    chars: Vec<char>,
    pos: usize,
    flags: u32,
    global_flags: u32,
    root_has_pattern: bool,
    group_count: usize,
    group_names: HashMap<String, usize>,
    group_widths: Vec<Option<Width>>,
    open_groups: Vec<usize>,
    group_ref_positions: HashMap<usize, usize>,
    lookbehind_groups: Option<usize>,
    lookbehind_checks: Vec<Expr>,
}

pub(super) fn parse(pattern: &str, flags: PyFlags) -> Result<ParsedPattern, CompileError> {
    if flags.0 & !KNOWN_FLAGS != 0 {
        return Err(re_error("unknown flag", None, pattern));
    }
    let mut parser = Parser {
        pattern,
        chars: pattern.chars().collect(),
        pos: 0,
        flags: flags.0,
        global_flags: flags.0,
        root_has_pattern: false,
        group_count: 0,
        group_names: HashMap::new(),
        group_widths: vec![None],
        open_groups: Vec::new(),
        group_ref_positions: HashMap::new(),
        lookbehind_groups: None,
        lookbehind_checks: Vec::new(),
    };
    let parsed = parser.parse_alternation(false, 0, 0);
    // CPython tokenizer 每取走一個 token 即預讀下一個；樣式以單獨 `\` 結尾時，取走倒數第二個 token
    // 的當下就拋 `bad escape (end of pattern)`，優先於該 token 之後才會判定的任何錯誤（含解析成功後的檢查）。
    // 解析器的位置已到達結尾反斜線（或解析完成）即表示 CPython 已預讀到它。
    if let Some(slash) = parser.trailing_lone_backslash() {
        if parsed.is_ok() || parser.pos >= slash {
            return Err(parser.error("bad escape (end of pattern)", Some(slash)));
        }
    }
    let expression = parsed?;
    if parser.pos < parser.chars.len() {
        return Err(parser.error("unbalanced parenthesis", Some(parser.pos)));
    }
    if let Some((group, pos)) = parser
        .group_ref_positions
        .iter()
        .filter(|(group, _)| **group > parser.group_count)
        .min_by_key(|(_, pos)| *pos)
        .map(|(group, pos)| (*group, *pos))
    {
        return Err(parser.error(&format!("invalid group reference {group}"), Some(pos)));
    }
    if parser
        .lookbehind_checks
        .iter()
        .any(|body| !is_fixed_width(body, &parser.group_widths))
    {
        return Err(parser.error("look-behind requires fixed-width pattern", None));
    }
    let mut final_flags = parser.global_flags;
    if final_flags & FLAG_L != 0 {
        return Err(value_error(
            "cannot use LOCALE flag with a str pattern",
            pattern,
        ));
    }
    if final_flags & FLAG_A == 0 {
        final_flags |= FLAG_U;
    } else if final_flags & FLAG_U != 0 {
        return Err(value_error(
            "ASCII and UNICODE flags are incompatible",
            pattern,
        ));
    }
    Ok(ParsedPattern {
        expression,
        global_flags: final_flags,
    })
}

impl Parser<'_> {
    fn parse_alternation(
        &mut self,
        in_group: bool,
        group_start: usize,
        depth: usize,
    ) -> Result<Expr, CompileError> {
        let mut branches = vec![self.parse_sequence(in_group, group_start, depth)?];
        loop {
            self.skip_ignored();
            if self.peek() != Some('|') {
                break;
            }
            if !in_group {
                self.root_has_pattern = true;
            }
            self.pos += 1;
            branches.push(self.parse_sequence(in_group, group_start, depth)?);
        }

        if in_group {
            self.skip_ignored();
            if self.peek() == Some(')') {
                self.pos += 1;
            } else {
                return Err(self.error("missing ), unterminated subpattern", Some(group_start)));
            }
        } else if self.peek() == Some(')') {
            return Err(self.error("unbalanced parenthesis", Some(self.pos)));
        }
        Ok(combine_alternation(branches))
    }

    fn parse_sequence(
        &mut self,
        in_group: bool,
        group_start: usize,
        depth: usize,
    ) -> Result<Expr, CompileError> {
        let mut items = Vec::new();
        let mut previous_was_repeat = false;

        loop {
            self.skip_ignored();
            let Some(ch) = self.peek() else {
                if in_group {
                    return Err(self.error("missing ), unterminated subpattern", Some(group_start)));
                }
                break;
            };
            if ch == '|' || ch == ')' {
                break;
            }

            if is_simple_repeat(ch) || ch == '{' {
                let repeat_start = self.pos;
                if let Some((min, max, mode)) = self.parse_repeat()? {
                    // CPython 先判定可否重複，之後才取走 `?`／`+` 修飾字元（單一字元）；
                    // 報錯時位置退回修飾字元之前，結尾反斜線的預讀判定才與 CPython 一致。
                    let modifier = usize::from(mode != RepeatMode::Greedy);
                    let Some(previous) = items.last() else {
                        self.pos -= modifier;
                        return Err(self.error("nothing to repeat", Some(repeat_start)));
                    };
                    if previous_was_repeat || matches!(previous, Expr::Repeat { .. }) {
                        self.pos -= modifier;
                        return Err(self.error("multiple repeat", Some(repeat_start)));
                    }
                    if matches!(previous, Expr::Anchor(_) | Expr::WordBoundary { .. }) {
                        self.pos -= modifier;
                        return Err(self.error("nothing to repeat", Some(repeat_start)));
                    }
                    let atom = items.pop().expect("已確認存在前一項");
                    items.push(Expr::Repeat {
                        body: Box::new(atom),
                        min,
                        max,
                        mode,
                    });
                    previous_was_repeat = true;
                    continue;
                }
            }

            let (mut atom, repeatable) = self.parse_atom(depth)?;
            if depth == 0 && atom != Expr::Empty {
                self.root_has_pattern = true;
            }
            if atom == Expr::Empty {
                continue;
            }

            self.skip_ignored();
            if repeatable {
                if let Some((min, max, mode)) = self.parse_repeat()? {
                    atom = Expr::Repeat {
                        body: Box::new(atom),
                        min,
                        max,
                        mode,
                    };
                    previous_was_repeat = true;
                } else {
                    previous_was_repeat = false;
                }
            } else {
                previous_was_repeat = false;
            }
            items.push(atom);
        }
        Ok(combine_sequence(items))
    }

    fn parse_atom(&mut self, depth: usize) -> Result<(Expr, bool), CompileError> {
        let pos = self.pos;
        let ch = self.next().expect("呼叫端已確認 pattern 未結束");
        match ch {
            '(' => self.parse_group(pos, depth),
            '[' => Ok((self.parse_character_class(pos)?, true)),
            '\\' => self.parse_escape(pos, false),
            '.' => Ok((Expr::Dot, true)),
            '^' => Ok((Expr::Anchor(Anchor::Start), false)),
            '$' => Ok((
                Expr::Anchor(Anchor::End {
                    multiline: self.flags & FLAG_M != 0,
                }),
                false,
            )),
            '*' | '+' | '?' => Err(self.error("nothing to repeat", Some(pos))),
            _ => Ok((Expr::Literal(ch), true)),
        }
    }

    fn parse_group(&mut self, open: usize, depth: usize) -> Result<(Expr, bool), CompileError> {
        let nested_depth = depth + 1;
        if nested_depth >= PARSE_DEPTH_LIMIT {
            return Err(CompileError::Recursion);
        }

        if self.peek() != Some('?') {
            return self.parse_capture_group(open, nested_depth, None);
        }
        self.pos += 1;
        let Some(kind) = self.peek() else {
            return Err(self.error("unknown extension ?", Some(open + 1)));
        };
        match kind {
            ':' => {
                self.pos += 1;
                let old_flags = self.flags;
                let body = self.parse_alternation(true, open, nested_depth)?;
                self.flags = old_flags;
                Ok((Expr::NonCapture(Box::new(body)), true))
            }
            '=' | '!' => {
                self.pos += 1;
                let body = self.parse_alternation(true, open, nested_depth)?;
                Ok((
                    Expr::LookAhead {
                        positive: kind == '=',
                        body: Box::new(body),
                    },
                    true,
                ))
            }
            '>' => {
                self.pos += 1;
                let body = self.parse_alternation(true, open, nested_depth)?;
                Ok((Expr::Atomic(Box::new(body)), true))
            }
            '#' => self.parse_comment_group(open),
            'P' => self.parse_python_group(open, nested_depth),
            '(' => self.parse_conditional(open, nested_depth),
            '<' => {
                self.pos += 1;
                match self.peek() {
                    Some('=') | Some('!') => {
                        let positive = self.next() == Some('=');
                        let previous_lookbehind_groups = self.lookbehind_groups;
                        if previous_lookbehind_groups.is_none() {
                            self.lookbehind_groups = Some(self.group_count);
                        }
                        let body = self.parse_alternation(true, open, nested_depth)?;
                        self.lookbehind_groups = previous_lookbehind_groups;
                        self.lookbehind_checks.push(body.clone());
                        Ok((
                            Expr::LookBehind {
                                positive,
                                body: Box::new(body),
                            },
                            true,
                        ))
                    }
                    _ => {
                        let suffix = self.peek().unwrap_or('\0');
                        self.consume_token();
                        let ext = format!("?<{suffix}");
                        Err(self.error(&format!("unknown extension {ext}"), Some(open + 1)))
                    }
                }
            }
            'a' | 'i' | 'L' | 'm' | 's' | 't' | 'u' | 'x' | '-' => {
                self.parse_flags_group(open, nested_depth)
            }
            _ => {
                self.consume_token();
                Err(self.error(&format!("unknown extension ?{kind}"), Some(open + 1)))
            }
        }
    }

    fn parse_capture_group(
        &mut self,
        open: usize,
        nested_depth: usize,
        name: Option<(String, usize)>,
    ) -> Result<(Expr, bool), CompileError> {
        self.group_count += 1;
        let index = self.group_count;
        if let Some((ref group_name, name_pos)) = name {
            if !valid_group_name(group_name) {
                return Err(self.error(
                    &format!("bad character in group name {}", python_repr(group_name)),
                    Some(name_pos),
                ));
            }
            if let Some(previous) = self.group_names.get(group_name) {
                return Err(self.error(
                    &format!(
                        "redefinition of group name {} as group {index}; was group {previous}",
                        python_repr(group_name)
                    ),
                    Some(name_pos),
                ));
            }
            self.group_names.insert(group_name.clone(), index);
        }

        if self.group_widths.len() <= index {
            self.group_widths.push(None);
        }
        self.open_groups.push(index);
        let old_flags = self.flags;
        let body = self.parse_alternation(true, open, nested_depth)?;
        self.flags = old_flags;
        self.open_groups.pop();
        self.group_widths[index] = Some(width_of(&body, &self.group_widths));
        Ok((
            Expr::Capture {
                index,
                body: Box::new(body),
            },
            true,
        ))
    }

    fn parse_comment_group(&mut self, open: usize) -> Result<(Expr, bool), CompileError> {
        self.pos += 1;
        while let Some(ch) = self.next() {
            if ch == ')' {
                return Ok((Expr::Empty, false));
            }
        }
        Err(self.error("missing ), unterminated comment", Some(open)))
    }

    fn parse_python_group(
        &mut self,
        open: usize,
        nested_depth: usize,
    ) -> Result<(Expr, bool), CompileError> {
        self.pos += 1; // P
        match self.peek() {
            Some('<') => {
                self.pos += 1;
                let name_pos = self.pos;
                let name = self.read_until('>');
                if self.peek() != Some('>') {
                    return Err(self.error("missing >, unterminated name", Some(open)));
                }
                if name.is_empty() {
                    self.pos += 1; // CPython getuntil 取走 `>` 後才報錯
                    return Err(self.error("missing group name", Some(name_pos)));
                }
                self.pos += 1;
                self.parse_capture_group(open, nested_depth, Some((name, name_pos)))
            }
            Some('=') => {
                self.pos += 1;
                let name_pos = self.pos;
                let name = self.read_until(')');
                if self.peek() != Some(')') {
                    return Err(self.error("missing ), unterminated name reference", Some(open)));
                }
                self.pos += 1;
                let Some(index) = self.group_names.get(&name).copied() else {
                    return Err(self.error(&format!("unknown group name '{name}'"), Some(name_pos)));
                };
                if self.open_groups.contains(&index) {
                    return Err(self.error("cannot refer to an open group", Some(name_pos)));
                }
                Ok((Expr::Backreference { index }, true))
            }
            _ => {
                self.consume_token();
                Err(self.error("unknown extension ?P", Some(open + 1)))
            }
        }
    }

    fn parse_conditional(
        &mut self,
        open: usize,
        nested_depth: usize,
    ) -> Result<(Expr, bool), CompileError> {
        self.pos += 1; // 條件式的第二個左括號
        let group_pos = self.pos;
        let group_text = self.read_until(')');
        if self.peek() != Some(')') {
            return Err(self.error("missing ), unterminated name reference", Some(open)));
        }
        self.pos += 1;
        let group = if !group_text.is_empty() && group_text.chars().all(|c| c.is_ascii_digit()) {
            let index = group_text.parse::<usize>().unwrap_or(usize::MAX);
            if index == 0 {
                return Err(self.error("bad group number", Some(self.pos)));
            }
            self.group_ref_positions.entry(index).or_insert(group_pos);
            index
        } else if let Some(index) = self.group_names.get(&group_text).copied() {
            index
        } else {
            return Err(self.error(
                &format!("unknown group name {}", python_repr(&group_text)),
                Some(group_pos),
            ));
        };
        if self.lookbehind_groups.is_some_and(|limit| group > limit) {
            return Err(self.error(
                "cannot refer to group defined in the same lookbehind subpattern",
                Some(self.pos),
            ));
        }

        let yes = self.parse_sequence(true, open, nested_depth)?;
        let mut no_branches = Vec::new();
        if self.peek() == Some('|') {
            self.pos += 1;
            no_branches.push(self.parse_sequence(true, open, nested_depth)?);
            while self.peek() == Some('|') {
                return Err(self.error(
                    "conditional backref with more than two branches",
                    Some(self.pos),
                ));
            }
        } else {
            no_branches.push(Expr::Empty);
        }
        if self.peek() != Some(')') {
            return Err(self.error("missing ), unterminated subpattern", Some(open)));
        }
        self.pos += 1;
        Ok((
            Expr::Conditional {
                group,
                yes: Box::new(yes),
                no: Box::new(combine_alternation(no_branches)),
            },
            true,
        ))
    }

    fn parse_flags_group(
        &mut self,
        open: usize,
        nested_depth: usize,
    ) -> Result<(Expr, bool), CompileError> {
        let mut add = 0u32;
        let mut remove = 0u32;
        let mut delimiter = self.next().expect("旗標群組至少含一個字元");
        if delimiter != '-' {
            loop {
                let Some(bit) = flag_value(delimiter) else {
                    let message = if delimiter.is_alphabetic() {
                        "unknown flag"
                    } else {
                        "missing -, : or )"
                    };
                    let at = self.pos.saturating_sub(1);
                    self.finish_backslash_token(delimiter);
                    return Err(self.error(message, Some(at)));
                };
                add |= bit;
                if bit & (FLAG_A | FLAG_L | FLAG_U) != 0 && add & (FLAG_A | FLAG_L | FLAG_U) != bit
                {
                    return Err(self.error(
                        "bad inline flags: flags 'a', 'u' and 'L' are incompatible",
                        Some(self.pos),
                    ));
                }
                if bit == FLAG_L {
                    return Err(self.error(
                        "bad inline flags: cannot use 'L' flag with a str pattern",
                        Some(self.pos),
                    ));
                }
                let Some(next) = self.next() else {
                    return Err(self.error("missing -, : or )", Some(self.pos)));
                };
                delimiter = next;
                if matches!(delimiter, ')' | ':' | '-') {
                    break;
                }
            }
        }

        if delimiter == ')' {
            if nested_depth != 1 || self.root_has_pattern {
                return Err(self.error(
                    "global flags not at the start of the expression",
                    Some(open),
                ));
            }
            self.flags = (self.flags | add) & !remove;
            self.global_flags = (self.global_flags | add) & !remove;
            return Ok((Expr::Empty, false));
        }

        if delimiter == '-' {
            let Some(mut flag) = self.next() else {
                return Err(self.error("missing flag", Some(self.pos)));
            };
            loop {
                let Some(bit) = flag_value(flag) else {
                    if flag == ')' {
                        return Err(self.error("missing flag", Some(self.pos.saturating_sub(1))));
                    }
                    let message = if flag.is_alphabetic() {
                        "unknown flag"
                    } else {
                        "missing flag"
                    };
                    let at = self.pos.saturating_sub(1);
                    self.finish_backslash_token(flag);
                    return Err(self.error(message, Some(at)));
                };
                if bit & (FLAG_A | FLAG_L | FLAG_U) != 0 {
                    return Err(self.error(
                        "bad inline flags: cannot turn off flags 'a', 'u' and 'L'",
                        Some(self.pos),
                    ));
                }
                remove |= bit;
                let Some(next) = self.next() else {
                    return Err(self.error("missing :", Some(self.pos)));
                };
                if next == ')' {
                    return Err(self.error("missing :", Some(self.pos.saturating_sub(1))));
                }
                if next == ':' {
                    delimiter = ':';
                    break;
                }
                flag = next;
            }
        }

        if delimiter != ':' {
            return Err(self.error("missing -, : or )", Some(self.pos)));
        }
        if add & FLAG_T != 0 {
            return Err(self.error(
                "bad inline flags: cannot turn on global flag",
                Some(self.pos.saturating_sub(1)),
            ));
        }
        if remove & FLAG_T != 0 {
            return Err(self.error(
                "bad inline flags: cannot turn off global flag",
                Some(self.pos.saturating_sub(1)),
            ));
        }
        if add & remove != 0 {
            return Err(self.error(
                "bad inline flags: flag turned on and off",
                Some(self.pos.saturating_sub(1)),
            ));
        }

        let old_flags = self.flags;
        self.flags = (self.flags | add) & !remove;
        let body = self.parse_alternation(true, open, nested_depth)?;
        self.flags = old_flags;
        Ok((
            Expr::ScopedFlags {
                add,
                remove,
                body: Box::new(body),
            },
            true,
        ))
    }

    fn parse_character_class(&mut self, open: usize) -> Result<Expr, CompileError> {
        let mut negated = false;
        if self.peek() == Some('^') {
            negated = true;
            self.pos += 1;
        }
        // 對照 CPython `sre_parse._parse` 的 `[` 分支：逐 token 讀取，讀到 `a-b` 當下就檢查範圍，
        // 所以 `bad character range` 先於其後的未封閉與跳脫錯誤（S1X-b-1）。
        let mut items = Vec::new();
        loop {
            let Some(ch) = self.peek() else {
                return Err(self.error("unterminated character set", Some(open)));
            };
            if ch == ']' && !items.is_empty() {
                self.pos += 1;
                break;
            }
            let first = self.parse_class_token()?;
            if self.peek() != Some('-') {
                items.push(first.item);
                continue;
            }
            self.pos += 1;
            match self.peek() {
                None => return Err(self.error("unterminated character set", Some(open))),
                Some(']') => {
                    self.pos += 1;
                    items.push(first.item);
                    items.push(ClassItem::Character('-'));
                    break;
                }
                Some(_) => {}
            }
            let second = self.parse_class_token()?;
            match (&first.item, &second.item) {
                (ClassItem::Character(start), ClassItem::Character(end)) if start <= end => {
                    items.push(ClassItem::Range(*start, *end));
                }
                _ => {
                    // CPython 位置為 `tell − (len(this) + 1 + len(that))`；跳脫的 token 原文只有兩個字元，
                    // 故 `\x7a-a` 這類範圍的位置不等於第一個 token 的起點。
                    let span = first.raw.chars().count() + 1 + second.raw.chars().count();
                    return Err(self.error(
                        &format!("bad character range {}-{}", first.raw, second.raw),
                        Some(self.pos.saturating_sub(span)),
                    ));
                }
            }
        }
        Ok(Expr::CharacterClass { negated, items })
    }

    /// 讀取字元類別內的一個 token（呼叫端保證未到 EOF）。
    fn parse_class_token(&mut self) -> Result<ClassToken, CompileError> {
        let start = self.pos;
        let ch = self.next().unwrap_or_default();
        if ch == '\\' {
            return self.parse_class_escape(start);
        }
        Ok(ClassToken {
            item: ClassItem::Character(ch),
            raw: ch.to_string(),
        })
    }

    fn parse_class_escape(&mut self, slash: usize) -> Result<ClassToken, CompileError> {
        let Some(ch) = self.next() else {
            return Err(self.error("bad escape (end of pattern)", Some(slash)));
        };
        let item = match ch {
            'd' | 'D' | 's' | 'S' | 'w' | 'W' => ClassItem::Category(self.category_for_escape(ch)),
            'b' => ClassItem::Character('\u{0008}'),
            'a' => ClassItem::Character('\u{0007}'),
            'f' => ClassItem::Character('\u{000c}'),
            'n' => ClassItem::Character('\n'),
            'r' => ClassItem::Character('\r'),
            't' => ClassItem::Character('\t'),
            'v' => ClassItem::Character('\u{000b}'),
            '\\' => ClassItem::Character('\\'),
            'x' => ClassItem::Character(self.parse_hex_escape(slash, 2)?),
            'u' => ClassItem::Character(self.parse_hex_escape(slash, 4)?),
            'U' => ClassItem::Character(self.parse_hex_escape(slash, 8)?),
            'N' => ClassItem::Character(self.parse_named_character(slash)?),
            '0'..='7' => ClassItem::Character(self.parse_octal_escape(slash, ch)?),
            '8' | '9' => return Err(self.error(&format!("bad escape \\{ch}"), Some(slash))),
            other if other.is_ascii_alphabetic() => {
                return Err(self.error(&format!("bad escape \\{other}"), Some(slash)));
            }
            other => ClassItem::Character(other),
        };
        Ok(ClassToken {
            item,
            raw: self.chars[slash..(slash + 2).min(self.pos)]
                .iter()
                .collect(),
        })
    }

    fn parse_escape(
        &mut self,
        slash: usize,
        _in_class: bool,
    ) -> Result<(Expr, bool), CompileError> {
        let Some(ch) = self.next() else {
            return Err(self.error("bad escape (end of pattern)", Some(slash)));
        };
        let expr = match ch {
            'a' => Expr::Literal('\u{0007}'),
            'f' => Expr::Literal('\u{000c}'),
            'n' => Expr::Literal('\n'),
            'r' => Expr::Literal('\r'),
            't' => Expr::Literal('\t'),
            'v' => Expr::Literal('\u{000b}'),
            '\\' => Expr::Literal('\\'),
            'x' => Expr::Literal(self.parse_hex_escape(slash, 2)?),
            'u' => Expr::Literal(self.parse_hex_escape(slash, 4)?),
            'U' => Expr::Literal(self.parse_hex_escape(slash, 8)?),
            'N' => Expr::Literal(self.parse_named_character(slash)?),
            '0' => Expr::Literal(self.parse_octal_escape(slash, '0')?),
            '1'..='9' => self.parse_numeric_escape(slash, ch)?,
            'd' | 'D' | 's' | 'S' | 'w' | 'W' => Expr::Category(self.category_for_escape(ch)),
            'A' => Expr::Anchor(Anchor::AbsoluteStart),
            'Z' => Expr::Anchor(Anchor::AbsoluteEnd),
            'b' => Expr::WordBoundary {
                boundary: true,
                ascii: self.flags & FLAG_A != 0,
            },
            'B' => Expr::WordBoundary {
                boundary: false,
                ascii: self.flags & FLAG_A != 0,
            },
            other if other.is_ascii_alphabetic() => {
                return Err(self.error(&format!("bad escape \\{other}"), Some(slash)));
            }
            other => Expr::Literal(other),
        };
        let repeatable = !matches!(expr, Expr::Anchor(_) | Expr::WordBoundary { .. });
        Ok((expr, repeatable))
    }

    fn parse_numeric_escape(&mut self, slash: usize, first: char) -> Result<Expr, CompileError> {
        let mut digits = first.to_string();
        if self.peek().is_some_and(|ch| ch.is_ascii_digit()) {
            digits.push(self.next().expect("已確認後續為數字"));
            if first <= '7'
                && digits.as_bytes()[1].is_ascii_digit()
                && ('0'..='7').contains(&(digits.as_bytes()[1] as char))
                && self.peek().is_some_and(|ch| ('0'..='7').contains(&ch))
            {
                digits.push(self.next().expect("已確認後續為八進位數字"));
                let value = u32::from_str_radix(&digits, 8).expect("最多三位八進位數字");
                if value > 0o377 {
                    return Err(self.error(
                        &format!("octal escape value \\{digits} outside of range 0-0o377"),
                        Some(slash),
                    ));
                }
                return Ok(Expr::Literal(char::from_u32(value).unwrap()));
            }
        }
        let group = digits.parse::<usize>().unwrap_or(usize::MAX);
        if group == 0 || group > self.group_count {
            return Err(self.error(&format!("invalid group reference {group}"), Some(slash + 1)));
        }
        if self.open_groups.contains(&group) {
            return Err(self.error("cannot refer to an open group", Some(slash)));
        }
        if self.lookbehind_groups.is_some_and(|limit| group > limit) {
            return Err(self.error(
                "cannot refer to group defined in the same lookbehind subpattern",
                Some(self.pos),
            ));
        }
        Ok(Expr::Backreference { index: group })
    }

    fn parse_hex_escape(&mut self, slash: usize, digits: usize) -> Result<char, CompileError> {
        let mut value = 0u32;
        // CPython `getwhile` 只取走符合的數字；不符的字元留在預讀位置，不算取走。
        for _ in 0..digits {
            let Some(d) = self.peek().and_then(|ch| ch.to_digit(16)) else {
                return Err(self.error("incomplete escape \\x", Some(slash)));
            };
            self.pos += 1;
            value = value * 16 + d;
        }
        if (0xd800..=0xdfff).contains(&value) {
            return Err(CompileError::Untranslatable("surrogate".to_string()));
        }
        char::from_u32(value).ok_or_else(|| self.error("bad escape \\U", Some(slash)))
    }

    fn parse_named_character(&mut self, slash: usize) -> Result<char, CompileError> {
        if self.peek() != Some('{') {
            return Err(self.error("missing { in character name", Some(slash)));
        }
        self.pos += 1;
        let name = self.read_until('}');
        if self.peek() != Some('}') {
            return Err(self.error("missing } in character name", Some(slash)));
        }
        self.pos += 1;
        super::unicode_tables::unicode_name(&name).ok_or_else(|| {
            self.error(
                &format!("undefined character name {}", python_repr(&name)),
                Some(slash),
            )
        })
    }

    fn parse_octal_escape(&mut self, slash: usize, first: char) -> Result<char, CompileError> {
        let mut value = first.to_digit(8).unwrap();
        let mut digits = first.to_string();
        let mut count = 1;
        while count < 3 && self.peek().is_some_and(|c| ('0'..='7').contains(&c)) {
            let next = self.next().unwrap();
            digits.push(next);
            value = value * 8 + next.to_digit(8).unwrap();
            count += 1;
        }
        if value > 0o377 {
            return Err(self.error(
                &format!("octal escape value \\{digits} outside of range 0-0o377"),
                Some(slash),
            ));
        }
        Ok(char::from_u32(value).expect("八進位逸出已限制在 0o377 內"))
    }

    fn category_for_escape(&self, escape: char) -> Category {
        let kind = match escape.to_ascii_lowercase() {
            'd' => CategoryKind::Digit,
            's' => CategoryKind::Space,
            _ => CategoryKind::Word,
        };
        Category {
            kind,
            negated: escape.is_ascii_uppercase(),
            ascii: self.flags & FLAG_A != 0,
        }
    }

    fn parse_repeat(&mut self) -> Result<Option<(usize, Option<usize>, RepeatMode)>, CompileError> {
        let (min, max) = match self.peek() {
            Some('*') => {
                self.pos += 1;
                (0, None)
            }
            Some('+') => {
                self.pos += 1;
                (1, None)
            }
            Some('?') => {
                self.pos += 1;
                (0, Some(1))
            }
            Some('{') => match self.parse_braced_repeat(self.pos)? {
                Some(value) => value,
                None => return Ok(None),
            },
            _ => return Ok(None),
        };
        let mode = match self.peek() {
            Some('?') => {
                self.pos += 1;
                RepeatMode::Lazy
            }
            Some('+') => {
                self.pos += 1;
                RepeatMode::Possessive
            }
            _ => RepeatMode::Greedy,
        };
        Ok(Some((min, max, mode)))
    }

    fn parse_braced_repeat(
        &mut self,
        start: usize,
    ) -> Result<Option<(usize, Option<usize>)>, CompileError> {
        let mut i = start + 1;
        if self.chars.get(i) == Some(&'}') {
            return Ok(None);
        }
        let min_start = i;
        while self.chars.get(i).is_some_and(|c| c.is_ascii_digit()) {
            i += 1;
        }
        let min_digits = &self.chars[min_start..i];
        let mut max_digits: Option<&[char]> = None;
        let mut is_range = false;
        if self.chars.get(i) == Some(&',') {
            is_range = true;
            i += 1;
            let max_start = i;
            while self.chars.get(i).is_some_and(|c| c.is_ascii_digit()) {
                i += 1;
            }
            if i > max_start {
                max_digits = Some(&self.chars[max_start..i]);
            }
        }
        if self.chars.get(i) != Some(&'}') || (min_digits.is_empty() && !is_range) {
            return Ok(None);
        }
        // CPython 取走 `}` 後才轉換次數並檢查大小。
        self.pos = i + 1;

        let parse_count = |digits: &[char]| -> Result<usize, CompileError> {
            let text: String = digits.iter().collect();
            match text.parse::<u64>() {
                Ok(value) if value < u32::MAX as u64 => Ok(value as usize),
                _ => Err(CompileError::Overflow(
                    "the repetition number is too large".to_string(),
                )),
            }
        };
        let min = if min_digits.is_empty() {
            0
        } else {
            parse_count(min_digits)?
        };
        let max = if is_range {
            max_digits.map(parse_count).transpose()?
        } else {
            Some(min)
        };
        if max.is_some_and(|max| min > max) {
            return Err(self.error("min repeat greater than max repeat", Some(start + 1)));
        }
        Ok(Some((min, max)))
    }

    fn skip_ignored(&mut self) {
        if self.flags & FLAG_X == 0 {
            return;
        }
        loop {
            match self.peek() {
                Some(' ' | '\t' | '\n' | '\r' | '\u{000c}' | '\u{000b}') => self.pos += 1,
                Some('#') => {
                    while let Some(ch) = self.next() {
                        if ch == '\n' {
                            break;
                        }
                    }
                }
                _ => break,
            }
        }
    }

    /// 樣式結尾為單獨的 `\`（tokenizer 自頭切分時落單）時回傳其位置。
    fn trailing_lone_backslash(&self) -> Option<usize> {
        let run = self.chars.iter().rev().take_while(|ch| **ch == '\\').count();
        (run % 2 == 1).then(|| self.chars.len() - 1)
    }

    fn error(&self, msg: &str, pos: Option<usize>) -> CompileError {
        re_error(msg, pos, self.pattern)
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn next(&mut self) -> Option<char> {
        let ch = self.peek()?;
        self.pos += 1;
        Some(ch)
    }

    /// 取走一個 CPython tokenizer token：`\` 連同其後一個字元，否則單一字元。
    fn consume_token(&mut self) {
        if let Some(ch) = self.next() {
            self.finish_backslash_token(ch);
        }
    }

    /// 剛取走的字元 `ch` 若為 `\`，連同其後一個字元一起取走（CPython 的跳脫 token 為兩個字元）。
    fn finish_backslash_token(&mut self, ch: char) {
        if ch == '\\' {
            self.next();
        }
    }

    fn read_until(&mut self, end: char) -> String {
        let start = self.pos;
        while self.peek().is_some_and(|ch| ch != end) {
            self.pos += 1;
        }
        self.chars[start..self.pos].iter().collect()
    }
}

fn re_error(msg: &str, pos: Option<usize>, pattern: &str) -> CompileError {
    let line_col = pos.filter(|_| pattern.contains('\n')).map(|pos| {
        let mut line = 1;
        let mut column = 1;
        for ch in pattern.chars().take(pos) {
            if ch == '\n' {
                line += 1;
                column = 1;
            } else {
                column += 1;
            }
        }
        (line, column)
    });
    CompileError::Re(PyReError {
        msg: msg.to_string(),
        pos,
        line_col,
        value_error: false,
    })
}

fn value_error(msg: &str, _pattern: &str) -> CompileError {
    CompileError::Re(PyReError {
        msg: msg.to_string(),
        pos: None,
        line_col: None,
        value_error: true,
    })
}

fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(value.len() + 2);
    out.push(quote);
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if ch == quote => {
                out.push('\\');
                out.push(ch);
            }
            ch if ch.is_control() => {
                if ch as u32 <= 0xff {
                    out.push_str(&format!("\\x{:02x}", ch as u32));
                } else if ch as u32 <= 0xffff {
                    out.push_str(&format!("\\u{:04x}", ch as u32));
                } else {
                    out.push_str(&format!("\\U{:08x}", ch as u32));
                }
            }
            _ => out.push(ch),
        }
    }
    out.push(quote);
    out
}

fn combine_sequence(items: Vec<Expr>) -> Expr {
    match items.len() {
        0 => Expr::Empty,
        1 => items.into_iter().next().unwrap(),
        _ => Expr::Sequence(items),
    }
}

fn combine_alternation(branches: Vec<Expr>) -> Expr {
    match branches.len() {
        0 => Expr::Empty,
        1 => branches.into_iter().next().unwrap(),
        _ => Expr::Alternation(branches),
    }
}

fn is_simple_repeat(ch: char) -> bool {
    matches!(ch, '*' | '+' | '?')
}

fn flag_value(ch: char) -> Option<u32> {
    match ch {
        't' => Some(FLAG_T),
        'i' => Some(FLAG_I),
        'L' => Some(FLAG_L),
        'm' => Some(FLAG_M),
        's' => Some(FLAG_S),
        'u' => Some(FLAG_U),
        'x' => Some(FLAG_X),
        'a' => Some(FLAG_A),
        _ => None,
    }
}

fn valid_group_name(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    (first == '_'
        || super::unicode_tables::contains(super::unicode_tables::IDENT_START_RANGES, first as u32))
        && chars.all(|ch| {
            ch == '_'
                || super::unicode_tables::contains(
                    super::unicode_tables::IDENT_CONTINUE_RANGES,
                    ch as u32,
                )
        })
}

fn width_of(expr: &Expr, groups: &[Option<Width>]) -> Width {
    match expr {
        Expr::Empty
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. }
        | Expr::LookAhead { .. }
        | Expr::LookBehind { .. } => Width::zero(),
        Expr::Literal(_) | Expr::Dot | Expr::Category(_) | Expr::CharacterClass { .. } => {
            Width::one()
        }
        Expr::Sequence(items) => {
            let mut width = Width::zero();
            for item in items {
                let next = width_of(item, groups);
                width.min = width.min.saturating_add(next.min);
                width.max = match (width.max, next.max) {
                    (Some(left), Some(right)) => Some(left.saturating_add(right)),
                    _ => None,
                };
            }
            width
        }
        Expr::Alternation(branches) => {
            let widths: Vec<Width> = branches.iter().map(|item| width_of(item, groups)).collect();
            Width {
                min: widths.iter().map(|item| item.min).min().unwrap_or(0),
                max: widths
                    .iter()
                    .try_fold(0usize, |max, item| Some(max.max(item.max?))),
            }
        }
        Expr::NonCapture(body)
        | Expr::Capture { body, .. }
        | Expr::Atomic(body)
        | Expr::ScopedFlags { body, .. } => width_of(body, groups),
        Expr::Backreference { index } => groups
            .get(*index)
            .and_then(|width| *width)
            .unwrap_or(Width { min: 0, max: None }),
        Expr::Conditional { yes, no, .. } => {
            let yes = width_of(yes, groups);
            let no = width_of(no, groups);
            Width {
                min: yes.min.min(no.min),
                max: match (yes.max, no.max) {
                    (Some(a), Some(b)) => Some(a.max(b)),
                    _ => None,
                },
            }
        }
        Expr::Repeat { body, min, max, .. } => {
            let inner = width_of(body, groups);
            Width {
                min: inner.min.saturating_mul(*min),
                max: match (inner.max, max) {
                    (Some(width), Some(count)) => Some(width.saturating_mul(*count)),
                    (Some(0), _) => Some(0),
                    _ => None,
                },
            }
        }
    }
}

fn is_fixed_width(expr: &Expr, groups: &[Option<Width>]) -> bool {
    let width = width_of(expr, groups);
    width.max == Some(width.min)
}
