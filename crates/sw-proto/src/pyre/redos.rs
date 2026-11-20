//! ReDoS 可疑樣式判定（P7 Task 11；`event_engine/schema.py` 的 `_ambiguity_score`／`_has_backref`／
//! `_maybe_redos_suspicious`，EVT-D-S15：以無 google-re2 的 Python 為基準）。
//!
//! 08 §0.3 原稿以 `crate::pyre::sre::SubPattern` 為參數；實際 P3 的 AST（`parse::ParsedPattern`）為 pyre 私有型別，
//! 不能出現在公開簽章，故公開 API 以 `(pattern, flags)` 為輸入（P7 Task 0 核對註記）。
//!
//! Python 的分數算在 `sre_parse` 的 AST 上：`_parse` 會把無 flags 的非捕獲群組攤平進外層序列（量詞 body 亦取其內容），
//! 而 `_parse_sub` 解析 alternation 時在攤平後的分支上再做兩個正規化：
//! (1) 所有分支首項相同時抽出為共同前綴（重複的比較只看首項 tuple；含 SubPattern 的節點以物件 identity
//! 比較，故重複、群組、lookaround 永不被抽出），(2) 每個分支都只剩一個 LITERAL 或非否定 IN 時整個轉成 IN。
//! 本檔在 P3 AST 上以相同規則計算，結果與 `contracts/golden/p7/redos.ndjson` 逐列相同。

use super::parse::{self, ClassItem, Expr, RepeatMode};
use super::{CompileError, PyFlags};

/// sre AST 的歧義分數（`_ambiguity_score`）：可變量詞與 alternation 為來源，固定 `{N>=2}` 重複有歧義的 body 計兩倍。
///
/// Python 為任意精度整數；此處以飽和運算封頂於 `u64::MAX`（巢狀 `{2}` 每層倍增，64 層即超過），超大分數仍判可疑。
pub fn ambiguity_score(pattern: &str, flags: PyFlags) -> Result<u64, CompileError> {
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

fn score(e: &Expr) -> u64 {
    score_items(&sub_pattern(e))
}

/// `sre_parse` 產出的 SubPattern 項目：可比較相等的 atom、alternation 正規化後的 BRANCH，以及含子 pattern 的容器
/// （`SUBPATTERN`／量詞／lookaround 等，以物件 identity 比較，永不相等）。
enum Item<'a> {
    Atom(Atom),
    Branch(Vec<Vec<Item<'a>>>),
    Node(&'a Expr),
}

/// 依 `sre_parse._parse`／`_parse_sub` 把 P3 AST 轉成 sre 的 SubPattern 項目序列：無 flags 的非捕獲群組攤平進外層
/// （`_parse` 尾段 unpack；量詞 body 取 `p`），alternation 在攤平後的分支上做共同前綴抽出與 IN 化。
fn sub_pattern(e: &Expr) -> Vec<Item<'_>> {
    let mut out = Vec::new();
    flatten(e, &mut out);
    out
}

fn flatten<'a>(e: &'a Expr, out: &mut Vec<Item<'a>>) {
    match e {
        Expr::Empty => {}
        Expr::Sequence(items) => items.iter().for_each(|it| flatten(it, out)),
        Expr::NonCapture(body) => flatten(body, out),
        Expr::Alternation(branches) => alternation(branches, out),
        other => out.push(match atom(other) {
            Some(a) => Item::Atom(a),
            None => Item::Node(other),
        }),
    }
}

/// `_parse_sub` 的分支正規化：(1) 所有分支首項為相同 atom 時抽出為前綴，(2) 每個分支都只剩一個 LITERAL 或非否定 IN
/// 時整個化為 IN（`_uniq` 去重），否則為 BRANCH。
fn alternation<'a>(branches: &'a [Expr], out: &mut Vec<Item<'a>>) {
    let mut items: Vec<Vec<Item<'a>>> = branches.iter().map(sub_pattern).collect();
    while let Some(Item::Atom(first)) = items[0].first() {
        let all_same = items
            .iter()
            .all(|it| matches!(it.first(), Some(Item::Atom(a)) if a == first));
        if !all_same {
            break;
        }
        let prefix = items[0].remove(0);
        for it in &mut items[1..] {
            it.remove(0);
        }
        out.push(prefix);
    }
    let mut set = Vec::new();
    for it in &items {
        match it.as_slice() {
            [Item::Atom(Atom::Literal(c))] => set.push(ClassItem::Character(*c)),
            [Item::Atom(Atom::In(members))] => set.extend(members.iter().cloned()),
            _ => {
                out.push(Item::Branch(items));
                return;
            }
        }
    }
    out.push(Item::Atom(Atom::In(uniq(set))));
}

fn score_items(items: &[Item<'_>]) -> u64 {
    items.iter().map(item_score).fold(0, u64::saturating_add)
}

fn item_score(item: &Item<'_>) -> u64 {
    match item {
        Item::Atom(_) => 0,
        Item::Branch(branches) => branches
            .iter()
            .map(|b| score_items(b))
            .max()
            .unwrap_or(0)
            .saturating_add(1),
        Item::Node(e) => node_score(e),
    }
}

fn node_score(e: &Expr) -> u64 {
    match e {
        Expr::Capture { body, .. } | Expr::ScopedFlags { body, .. } | Expr::Atomic(body) => {
            score(body)
        }
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
                body_score.saturating_add(1)
            } else if at_least_twice {
                body_score.saturating_mul(2)
            } else {
                body_score
            }
        }
        // atom 與可攤平節點已在 `flatten` 處理。
        _ => 0,
    }
}

/// `sre_parse` 的 atom 正規形：只有不含子 pattern 的節點可比較相等。
#[derive(Debug, PartialEq)]
enum Atom {
    Literal(char),
    NotLiteral(char),
    In(Vec<ClassItem>),
    InNegated(Vec<ClassItem>),
    Any,
    At(Expr),
    GroupRef(usize),
}

fn atom(e: &Expr) -> Option<Atom> {
    Some(match e {
        Expr::Literal(c) => Atom::Literal(*c),
        // sre_parse：字元類別先 `_uniq`，只剩單一字元時化為 LITERAL（否定為 NOT_LITERAL）。
        Expr::CharacterClass { negated, items } => match (uniq(items.to_vec()).as_slice(), negated)
        {
            ([ClassItem::Character(c)], false) => Atom::Literal(*c),
            ([ClassItem::Character(c)], true) => Atom::NotLiteral(*c),
            (set, false) => Atom::In(set.to_vec()),
            (set, true) => Atom::InNegated(set.to_vec()),
        },
        // 類別外的 `\d` 等為 IN [(CATEGORY, …)]，與 `[\d]` 相等。
        Expr::Category(c) => Atom::In(vec![ClassItem::Category(*c)]),
        Expr::Dot => Atom::Any,
        Expr::Anchor(_) | Expr::WordBoundary { .. } => Atom::At(e.clone()),
        Expr::Backreference { index } => Atom::GroupRef(*index),
        _ => return None,
    })
}

/// `sre_parse._uniq`：保序去重。
fn uniq(items: Vec<ClassItem>) -> Vec<ClassItem> {
    let mut out: Vec<ClassItem> = Vec::with_capacity(items.len());
    for it in items {
        if !out.contains(&it) {
            out.push(it);
        }
    }
    out
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
