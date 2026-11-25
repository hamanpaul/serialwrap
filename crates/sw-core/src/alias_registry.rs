//! alias 與 binding 的持久化登錄（Slice 2.1 Task 15；`state.json` 的 aliases 區段）。
//!
//! Python 對照：`sw_core/alias_registry.py`。Rust 額外保留 alias 來源，讓 dynamic session
//! 在重啟與 template 變更後可區分自動名稱、session 自訂名稱與 by-id 自訂名稱。舊列沒有
//! `source` 時，依 `<template>+<數字>` 慣例推定自動名稱；未知欄位仍保留，0.5.0 可讀取新列。

use indexmap::IndexMap;
use sw_proto::pyjson::{PyDict, PyValue};

const SOURCE_FIELD: &str = "source";
const ACTIVE_FIELD: &str = "active";
const SOURCE_AUTOMATIC: &str = "automatic";
const SOURCE_SESSION: &str = "session";
const SOURCE_BY_ID: &str = "by_id";
const SOURCE_YAML: &str = "yaml";
const CONFLICTING_DEVICE_BY_IDS_FIELD: &str = "conflicting_device_by_ids";
const CONFLICTING_SESSION_IDS_FIELD: &str = "conflicting_session_ids";

/// alias 目前採用的來源種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AliasKind {
    ById,
    Session,
    Automatic,
}

/// 一個 session 依登錄表解析出的 alias。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AliasChoice {
    pub alias: String,
    pub kind: AliasKind,
}

/// alias 登錄表（`IndexMap<alias, PyDict>`，保留持久化列順序）。
#[derive(Clone, Debug, Default)]
pub struct AliasRegistry {
    rows: IndexMap<String, PyDict>,
}

impl AliasRegistry {
    /// `load(rows)`：整表取代（每列淺複製）。
    pub fn load(rows: IndexMap<String, PyDict>) -> AliasRegistry {
        AliasRegistry { rows }
    }

    /// `dump()`：整表複製（存檔用）。
    pub fn dump(&self) -> IndexMap<String, PyDict> {
        self.rows.clone()
    }

    /// `list_alias()`：依 alias 字典序，每列複製並加上 `"alias"` 鍵。
    pub fn list_alias(&self) -> Vec<PyDict> {
        let mut items: Vec<(&String, &PyDict)> = self.rows.iter().collect();
        items.sort_by(|a, b| a.0.cmp(b.0));
        items
            .into_iter()
            .map(|(alias, row)| {
                let mut rec = row.clone();
                rec.0.shift_remove(SOURCE_FIELD);
                rec.0.shift_remove(ACTIVE_FIELD);
                rec.0.shift_remove(CONFLICTING_DEVICE_BY_IDS_FIELD);
                rec.0.shift_remove(CONFLICTING_SESSION_IDS_FIELD);
                rec.insert("alias", PyValue::Str(alias.clone()));
                rec
            })
            .collect()
    }

    /// 取得指定名稱的持久化列。
    pub fn row(&self, alias: &str) -> Option<&PyDict> {
        self.rows.get(alias)
    }

    /// 判斷列是否為 `alias assign --by-id`，包含沒有來源欄位的舊格式列。
    pub fn is_by_id_assignment(&self, alias: &str) -> bool {
        self.rows.get(alias).is_some_and(|row| {
            is_active(row)
                && row.get("device_by_id").and_then(PyValue::as_str).is_some()
                && row.get("session_id").and_then(PyValue::as_str).is_none()
                && !matches!(source(row), Some(SOURCE_SESSION | SOURCE_YAML))
        })
    }

    /// alias 列是否仍是該名稱的有效設定；舊列沒有 active 欄時視為有效。
    pub fn is_active_alias(&self, alias: &str) -> bool {
        self.rows.get(alias).is_some_and(is_active)
    }

    /// 判斷列是否由目前版本明確寫成自動 alias；無來源的舊列不回報為 true。
    pub fn is_explicit_automatic_session_alias(&self, alias: &str) -> bool {
        self.rows
            .get(alias)
            .is_some_and(|row| source(row) == Some(SOURCE_AUTOMATIC))
    }

    /// 判斷同名列是否已有另一個 session／裝置宣告。
    pub fn conflicts_with_claim(&self, alias: &str, sid: &str, by_id: &str) -> bool {
        let Some(row) = self.rows.get(alias).filter(|row| is_active(row)) else {
            return false;
        };
        let owner_sid = row.get("session_id").and_then(PyValue::as_str);
        let owner_by_id = row.get("device_by_id").and_then(PyValue::as_str);
        let same_session = owner_sid == Some(sid)
            && owner_by_id.is_none_or(|owner| owner == by_id);
        let same_device = !by_id.is_empty() && owner_by_id == Some(by_id);
        if same_session || same_device {
            return false;
        }
        let recorded_conflict = has_by_id_conflict(row)
            || row
                .get(CONFLICTING_SESSION_IDS_FIELD)
                .and_then(|value| value.as_array())
                .is_some_and(|ids| !ids.is_empty());
        recorded_conflict || owner_sid.is_some() || owner_by_id.is_some()
    }

    /// 寫入自動產生的 session alias。
    pub fn set_for_session(&mut self, sid: &str, alias: &str, now_iso: &str) {
        let mut row = PyDict::new();
        row.insert("session_id", PyValue::Str(sid.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        self.rows.insert(alias.to_string(), row);
    }

    /// 寫入帶有明確來源的新版自動 alias。
    pub fn set_automatic_for_session(&mut self, sid: &str, alias: &str, now_iso: &str) {
        self.set_for_session_with_source(sid, alias, SOURCE_AUTOMATIC, now_iso);
    }

    /// 寫入帶來源的 session alias。額外來源欄位是向後相容的 JSON object 欄位。
    pub fn set_for_session_with_source(
        &mut self,
        sid: &str,
        alias: &str,
        source: &str,
        now_iso: &str,
    ) {
        let mut row = PyDict::new();
        row.insert("session_id", PyValue::Str(sid.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        row.insert(SOURCE_FIELD, PyValue::Str(source.to_string()));
        self.rows.insert(alias.to_string(), row);
    }

    /// 寫入使用者設定的 session alias。
    pub fn set_custom_for_session(
        &mut self,
        sid: &str,
        alias: &str,
        by_id: Option<&str>,
        now_iso: &str,
    ) {
        for (name, row) in &mut self.rows {
            if name != alias
                && row.get("session_id").and_then(PyValue::as_str) == Some(sid)
                && is_custom_session_row(name, row)
            {
                row.insert(ACTIVE_FIELD, PyValue::Bool(false));
            }
        }
        self.set_for_session_with_source(sid, alias, SOURCE_SESSION, now_iso);
        if let Some(by_id) = by_id.filter(|value| !value.is_empty()) {
            if let Some(row) = self.rows.get_mut(alias) {
                row.insert("device_by_id", PyValue::Str(by_id.to_string()));
            }
        }
    }

    /// 寫入 YAML target 的既有 alias。
    pub fn set_for_yaml_session(&mut self, sid: &str, alias: &str, now_iso: &str) {
        self.set_for_session_with_source(sid, alias, SOURCE_YAML, now_iso);
    }

    /// `assign_by_id(by_id, alias, profile)`：`{"device_by_id", "updated_at"}`，profile 為真值時加 `"profile"`。
    pub fn assign_by_id(&mut self, by_id: &str, alias: &str, profile: Option<&str>, now_iso: &str) {
        if let Some(row) = self.rows.get_mut(alias) {
            if is_active(row)
                && row.get("device_by_id").and_then(PyValue::as_str) != Some(by_id)
                && (row.get("session_id").and_then(PyValue::as_str).is_some()
                    || row.get("device_by_id").and_then(PyValue::as_str).is_some())
            {
                let mut conflicts = row
                    .get(CONFLICTING_DEVICE_BY_IDS_FIELD)
                    .and_then(|value| value.as_array())
                    .cloned()
                    .unwrap_or_default();
                if !conflicts.iter().any(|value| value.as_str() == Some(by_id)) {
                    conflicts.push(PyValue::Str(by_id.to_string()));
                }
                row.insert(CONFLICTING_DEVICE_BY_IDS_FIELD, PyValue::List(conflicts));
                return;
            }
        }
        for (name, row) in &mut self.rows {
            if name != alias
                && row.get("device_by_id").and_then(PyValue::as_str) == Some(by_id)
                && !matches!(source(row), Some(SOURCE_YAML | SOURCE_SESSION))
            {
                row.insert(ACTIVE_FIELD, PyValue::Bool(false));
            }
        }
        let mut row = PyDict::new();
        row.insert("device_by_id", PyValue::Str(by_id.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        row.insert(SOURCE_FIELD, PyValue::Str(SOURCE_BY_ID.to_string()));
        if let Some(p) = profile.filter(|p| !p.is_empty()) {
            row.insert("profile", PyValue::Str(p.to_string()));
        }
        self.rows.insert(alias.to_string(), row);
    }

    /// 為舊版 session 自訂列補上動態 session 的裝置關聯，保留原 alias key 與其餘欄位。
    pub fn associate_custom_session_with_device(
        &mut self,
        alias: &str,
        sid: &str,
        by_id: &str,
        now_iso: &str,
    ) -> bool {
        let Some(row) = self.rows.get_mut(alias) else {
            return false;
        };
        if row.get("session_id").and_then(PyValue::as_str) != Some(sid)
            || !is_custom_session_row(alias, row)
        {
            return false;
        }
        row.insert("source", PyValue::Str(SOURCE_SESSION.to_string()));
        row.insert("device_by_id", PyValue::Str(by_id.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        true
    }

    /// 保留既有名稱的首個登錄列，記錄另一個 session 對同名 alias 的衝突宣告。
    pub fn record_conflicting_session(&mut self, alias: &str, sid: &str, by_id: &str) -> bool {
        let Some(row) = self.rows.get_mut(alias) else {
            return false;
        };
        let row_by_id = row.get("device_by_id").and_then(PyValue::as_str);
        let same_session = row.get("session_id").and_then(PyValue::as_str) == Some(sid)
            && row_by_id.is_none_or(|owner| owner == by_id);
        if !is_active(row) || same_session || (!by_id.is_empty() && row_by_id == Some(by_id)) {
            return false;
        }
        let mut conflicts = row
            .get(CONFLICTING_SESSION_IDS_FIELD)
            .and_then(|value| value.as_array())
            .cloned()
            .unwrap_or_default();
        if !conflicts
            .iter()
            .any(|value| value.as_str() == Some(sid))
        {
            conflicts.push(PyValue::Str(sid.to_string()));
        }
        row.insert(CONFLICTING_SESSION_IDS_FIELD, PyValue::List(conflicts));
        true
    }

    /// template 切換時將被採用的 session 自訂列 re-key，並保留其他持久化欄位。
    pub fn rekey_custom_session(
        &mut self,
        alias: &str,
        old_sid: &str,
        new_sid: &str,
        by_id: &str,
        now_iso: &str,
    ) -> bool {
        let Some(row) = self.rows.get_mut(alias) else {
            return false;
        };
        if row.get("session_id").and_then(PyValue::as_str) != Some(old_sid)
            || !is_custom_session_row(alias, row)
        {
            return false;
        }
        row.insert("session_id", PyValue::Str(new_sid.to_string()));
        row.insert("source", PyValue::Str(SOURCE_SESSION.to_string()));
        row.insert("device_by_id", PyValue::Str(by_id.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        true
    }

    /// 依 OD-112 的優先序解析 dynamic／pin／RELEASED session 的 alias。
    ///
    /// 舊 registry 列依名稱推定來源；查找候選時按 `updated_at` 再按 alias 名排序，
    /// 不依賴 `IndexMap` 插入順序。
    pub fn resolve_for_session(
        &self,
        sid: &str,
        by_id: &str,
        automatic_alias: &str,
    ) -> AliasChoice {
        if !by_id.is_empty() {
            if let Some((alias, _)) = self.newest_matching(|_, row| {
                let direct = row.get("device_by_id").and_then(PyValue::as_str) == Some(by_id)
                    && !matches!(source(row), Some(SOURCE_YAML | SOURCE_SESSION));
                direct && is_active(row)
            }) {
                return AliasChoice {
                    alias: alias.clone(),
                    kind: AliasKind::ById,
                };
            }
        }

        if let Some((alias, _)) = self.newest_matching(|alias, row| {
            let row_by_id = row.get("device_by_id").and_then(PyValue::as_str);
            let session_id_matches = row.get("session_id").and_then(PyValue::as_str)
                == Some(sid)
                && row_by_id.is_none_or(|owner| owner == by_id);
            let device_matches = source(row) == Some(SOURCE_SESSION)
                && !by_id.is_empty()
                && row_by_id == Some(by_id);
            (session_id_matches || device_matches)
                && is_custom_session_row(alias, row)
                && is_active(row)
        }) {
            return AliasChoice {
                alias: alias.clone(),
                kind: AliasKind::Session,
            };
        }

        if let Some((alias, _)) = self.newest_matching(|alias, row| {
            row.get("session_id").and_then(PyValue::as_str) == Some(sid)
                && is_automatic_session_row(alias, row)
                && is_active(row)
        }) {
            return AliasChoice {
                alias: alias.clone(),
                kind: AliasKind::Automatic,
            };
        }

        AliasChoice {
            alias: automatic_alias.to_string(),
            kind: AliasKind::Automatic,
        }
    }

    /// 最新的符合 predicate 的列；時間相同時以 alias 字典序定序。
    fn newest_matching(
        &self,
        mut predicate: impl FnMut(&str, &PyDict) -> bool,
    ) -> Option<(&String, &PyDict)> {
        let mut matches: Vec<(&String, &PyDict)> = self
            .rows
            .iter()
            .filter(|(alias, row)| predicate(alias, row))
            .collect();
        matches.sort_by(|(name_a, row_a), (name_b, row_b)| {
            updated_at(row_a)
                .cmp(updated_at(row_b))
                .then_with(|| name_a.cmp(name_b))
        });
        matches.pop()
    }

    /// 回傳是否確實移除。
    pub fn unassign(&mut self, alias: &str) -> bool {
        self.rows.shift_remove(alias).is_some()
    }
}

/// 舊列沒有來源欄時，符合 `<template>+<數字>` 才視為自動 alias。
pub fn is_automatic_alias_name(alias: &str) -> bool {
    let Some((template, suffix)) = alias.rsplit_once('+') else {
        return false;
    };
    !template.is_empty() && !suffix.is_empty() && suffix.chars().all(|c| c.is_ascii_digit())
}

/// alias row 目前是否由指定 by-id 裝置擁有。
pub fn is_by_id_row(row: &PyDict, by_id: &str) -> bool {
    row.get("device_by_id").and_then(PyValue::as_str) == Some(by_id)
}

/// alias 列是否記錄了其他裝置對同名 alias 的 by-id 指派。
pub fn has_by_id_conflict(row: &PyDict) -> bool {
    row.get(CONFLICTING_DEVICE_BY_IDS_FIELD)
        .and_then(|value| value.as_array())
        .is_some_and(|ids| !ids.is_empty())
}

/// alias 列是否有任一類型的重名衝突。
pub fn has_conflict(row: &PyDict) -> bool {
    has_by_id_conflict(row)
        || row
            .get(CONFLICTING_SESSION_IDS_FIELD)
            .and_then(|value| value.as_array())
            .is_some_and(|ids| !ids.is_empty())
}

fn source(row: &PyDict) -> Option<&str> {
    row.get(SOURCE_FIELD).and_then(PyValue::as_str)
}

fn is_active(row: &PyDict) -> bool {
    row.get(ACTIVE_FIELD).and_then(PyValue::as_bool) != Some(false)
}

fn is_custom_session_row(alias: &str, row: &PyDict) -> bool {
    match source(row) {
        Some(SOURCE_SESSION) => true,
        Some(SOURCE_AUTOMATIC | SOURCE_BY_ID | SOURCE_YAML) => false,
        Some(_) => true,
        None => !is_automatic_alias_name(alias),
    }
}

fn is_automatic_session_row(alias: &str, row: &PyDict) -> bool {
    match source(row) {
        Some(SOURCE_AUTOMATIC) => true,
        Some(_) => false,
        None => is_automatic_alias_name(alias),
    }
}

/// 判斷資料列是否為自動 session alias（包含依舊格式名稱推定的列）。
pub fn is_automatic_session_alias(alias: &str, row: &PyDict) -> bool {
    is_automatic_session_row(alias, row)
}

fn updated_at(row: &PyDict) -> &str {
    row.get("updated_at")
        .and_then(PyValue::as_str)
        .unwrap_or("")
}
