//! alias 與 binding 的持久化登錄（Slice 2.1 Task 15；`state.json` 的 aliases 區段）。
//!
//! Python 對照：`sw_core/alias_registry.py`（逐字承接；`now_iso` 由呼叫端提供以保持 sans-IO）。

use indexmap::IndexMap;
use sw_proto::pyjson::{PyDict, PyValue};

/// alias 登錄表（`IndexMap<alias, PyDict>`，保留插入順序以對應 Python dict）。
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
                rec.insert("alias", PyValue::Str(alias.clone()));
                rec
            })
            .collect()
    }

    /// `set_for_session(sid, alias)`：該列改為 `{"session_id", "updated_at"}`。
    pub fn set_for_session(&mut self, sid: &str, alias: &str, now_iso: &str) {
        let mut row = PyDict::new();
        row.insert("session_id", PyValue::Str(sid.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        self.rows.insert(alias.to_string(), row);
    }

    /// `assign_by_id(by_id, alias, profile)`：`{"device_by_id", "updated_at"}`，profile 為真值時加 `"profile"`。
    pub fn assign_by_id(&mut self, by_id: &str, alias: &str, profile: Option<&str>, now_iso: &str) {
        let mut row = PyDict::new();
        row.insert("device_by_id", PyValue::Str(by_id.to_string()));
        row.insert("updated_at", PyValue::Str(now_iso.to_string()));
        if let Some(p) = profile.filter(|p| !p.is_empty()) {
            row.insert("profile", PyValue::Str(p.to_string()));
        }
        self.rows.insert(alias.to_string(), row);
    }

    /// 回傳是否確實移除。
    pub fn unassign(&mut self, alias: &str) -> bool {
        self.rows.shift_remove(alias).is_some()
    }
}
