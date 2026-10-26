//! ReDoS 可疑樣式判定（P7 Task 11；`event_engine/schema.py` 的 `_ambiguity_score`／`_has_backref`／
//! `_maybe_redos_suspicious`，EVT-D-S15：以無 google-re2 的 Python 為基準）。
//!
//! 08 §0.3 原稿以 `crate::pyre::sre::SubPattern` 為參數；實際 P3 的 AST（`parse::ParsedPattern`）為 pyre 私有型別，
//! 不能出現在公開簽章，故公開 API 以 `(pattern, flags)` 為輸入（P7 Task 0 核對註記）。
//!
//! Python 的分數算在 `sre_parse` 的 AST 上，而 `sre_parse._parse_sub` 解析 alternation 時會先做兩個正規化：
//! (1) 所有分支首項相同時抽出為共同前綴（重複的比較只看首項 tuple；含 SubPattern 的節點以物件 identity
//! 比較，故重複、群組、lookaround 永不被抽出），(2) 每個分支都只剩一個 LITERAL 或非否定 IN 時整個轉成 IN。
//! 本檔在 P3 AST 上以相同規則計算，結果與 `contracts/golden/p7/redos.ndjson` 逐列相同。

use super::parse::{self, ClassItem, Expr, RepeatMode};
use super::{CompileError, PyFlags};

/// sre AST 的歧義分數（`_ambiguity_score`）：可變量詞與 alternation 為來源，固定 `{N>=2}` 重複有歧義的 body 計兩倍。
pub fn ambiguity_score(pattern: &str, flags: PyFlags) -> Result<u32, CompileError> {
    Ok(score(&parse::parse(pattern, flags)?.expression))
}

/// 是否含 back-reference（`GROUPREF`；條件式 `GROUPREF_EXISTS` 不算）。
pub fn has_backref(pattern: &str, flags: PyFlags) -> Result<bool, CompileError> {
    Ok(contains_backref(&parse::parse(pattern, flags)?.expression))
}

/// `score >= 2 || has_backref`（`_maybe_redos_suspicious`）。
pub fn maybe_redos_suspicious(pattern: &str, flags: PyFlags) -> Result<bool, CompileError> {
    let expr = parse::parse(pattern, flags)?.expression;
    Ok(score(&expr) >= 2 || contains_backref(&expr))
}

impl PyFlags {
    /// 事件規則 `pattern.flags` 字串（`i`／`s`／`m`）→ 旗標；其他字元回 `Err(ch)`（`_flags_from_string`）。
    pub fn from_rule_flags(s: &str) -> Result<PyFlags, char> {
        let mut out = 0;
        for ch in s.chars() {
            out |= match ch {
                'i' => PyFlags::I,
                's' => PyFlags::S,
                'm' => PyFlags::M,
                other => return Err(other),
            };
        }
        Ok(PyFlags(out))
    }
}

fn score(e: &Expr) -> u32 {
    match e {
        Expr::Sequence(items) => items.iter().map(score).sum(),
        Expr::Alternation(branches) => alternation_score(branches),
        Expr::NonCapture(body) | Expr::Atomic(body) => score(body),
        Expr::Capture { body, .. } | Expr::ScopedFlags { body, .. } => score(body),
        Expr::LookAhead { body, .. } | Expr::LookBehind { body, .. } => score(body),
        // GROUPREF_EXISTS：單一路徑只走一支，取 max。
        Expr::Conditional { yes, no, .. } => score(yes).max(score(no)),
        Expr::Repeat {
            body,
            min,
            max,
            mode,
        } => {
            let body_score = score(body);
            if *mode == RepeatMode::Possessive {
                return body_score;
            }
            let variable = max.is_none_or(|mx| mx != *min);
            let at_least_twice = max.is_none_or(|mx| mx >= 2);
            if variable {
                1 + body_score
            } else if at_least_twice {
                2 * body_score
            } else {
                body_score
            }
        }
        Expr::Empty
        | Expr::Literal(_)
        | Expr::Dot
        | Expr::Category(_)
        | Expr::CharacterClass { .. }
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. }
        | Expr::Backreference { .. } => 0,
    }
}

/// `sre_parse` 的分支首項正規形：只有不含子 pattern 的節點可比較相等（其餘以 identity 比較，永不相等）。
#[derive(Debug, PartialEq)]
enum Atom<'a> {
    Literal(char),
    NotLiteral(char),
    In(Vec<ClassItem>),
    InNegated(&'a [ClassItem]),
    Any,
    At(&'a Expr),
    GroupRef(usize),
}

fn atom(e: &Expr) -> Option<Atom<'_>> {
    Some(match e {
        Expr::Literal(c) => Atom::Literal(*c),
        // sre_parse：單一字元的字元類別 `[a]` 化為 LITERAL（否定為 NOT_LITERAL）。
        Expr::CharacterClass {
            negated: false,
            items,
        } if is_single_char(items) => Atom::Literal(single_char(items)),
        Expr::CharacterClass {
            negated: true,
            items,
        } if is_single_char(items) => Atom::NotLiteral(single_char(items)),
        Expr::CharacterClass {
            negated: false,
            items,
        } => Atom::In(items.clone()),
        Expr::CharacterClass {
            negated: true,
            items,
        } => Atom::InNegated(items),
        // 類別外的 `\d` 等為 IN [(CATEGORY, …)]，與 `[\d]` 相等。
        Expr::Category(c) => Atom::In(vec![ClassItem::Category(*c)]),
        Expr::Dot => Atom::Any,
        Expr::Anchor(_) | Expr::WordBoundary { .. } => Atom::At(e),
        Expr::Backreference { index } => Atom::GroupRef(*index),
        _ => return None,
    })
}

fn is_single_char(items: &[ClassItem]) -> bool {
    matches!(items, [ClassItem::Character(_)])
}

fn single_char(items: &[ClassItem]) -> char {
    match items {
        [ClassItem::Character(c)] => *c,
        _ => unreachable!("呼叫端已確認為單一字元"),
    }
}

fn branch_items(e: &Expr) -> &[Expr] {
    match e {
        Expr::Sequence(items) => items,
        Expr::Empty => &[],
        other => std::slice::from_ref(other),
    }
}

fn alternation_score(branches: &[Expr]) -> u32 {
    let mut items: Vec<&[Expr]> = branches.iter().map(branch_items).collect();
    // (1) 共同前綴抽出（被抽出的前綴必為不含子 pattern 的 atom，分數為 0）。
    while let Some(first) = items.first().and_then(|it| it.first()).and_then(atom) {
        let all_same = items
            .iter()
            .all(|it| it.first().and_then(atom).is_some_and(|a| a == first));
        if !all_same {
            break;
        }
        for it in &mut items {
            *it = &it[1..];
        }
    }
    // (2) 每個分支都只剩一個 LITERAL 或非否定 IN → 整個 alternation 化為 IN（不再是歧義來源）。
    let as_set = items
        .iter()
        .all(|it| matches!(it, [one] if matches!(atom(one), Some(Atom::Literal(_) | Atom::In(_)))));
    if as_set {
        return 0;
    }
    1 + items
        .iter()
        .map(|it| it.iter().map(score).sum::<u32>())
        .max()
        .unwrap_or(0)
}

fn contains_backref(e: &Expr) -> bool {
    match e {
        Expr::Backreference { .. } => true,
        Expr::Sequence(items) | Expr::Alternation(items) => items.iter().any(contains_backref),
        Expr::NonCapture(body) | Expr::Atomic(body) => contains_backref(body),
        Expr::Capture { body, .. } | Expr::ScopedFlags { body, .. } => contains_backref(body),
        Expr::LookAhead { body, .. } | Expr::LookBehind { body, .. } => contains_backref(body),
        Expr::Repeat { body, .. } => contains_backref(body),
        Expr::Conditional { yes, no, .. } => contains_backref(yes) || contains_backref(no),
        Expr::Empty
        | Expr::Literal(_)
        | Expr::Dot
        | Expr::Category(_)
        | Expr::CharacterClass { .. }
        | Expr::Anchor(_)
        | Expr::WordBoundary { .. } => false,
    }
}
