//! alias 與 binding 的持久化登錄（Slice 2.1 Task 15；`state.json` 的 aliases／bindings 區段）。
//!
//! Task 0 骨架：型別與簽章凍結，行為由 Task 15 交付。

use indexmap::IndexMap;
use sw_proto::pyjson::PyDict;

/// alias 登錄表（`IndexMap<alias, PyDict>`，保留插入順序以對應 Python dict）。
#[derive(Clone, Debug, Default)]
pub struct AliasRegistry {
    _rows: IndexMap<String, PyDict>,
}

impl AliasRegistry {
    pub fn load(_rows: IndexMap<String, PyDict>) -> AliasRegistry {
        crate::s21_todo!("T15", "AliasRegistry::load")
    }
    pub fn dump(&self) -> IndexMap<String, PyDict> {
        crate::s21_todo!("T15", "AliasRegistry::dump")
    }
    pub fn list_alias(&self) -> Vec<PyDict> {
        crate::s21_todo!("T15", "AliasRegistry::list_alias")
    }
    pub fn set_for_session(&mut self, _sid: &str, _alias: &str, _now_iso: &str) {
        crate::s21_todo!("T15", "AliasRegistry::set_for_session")
    }
    pub fn assign_by_id(&mut self, _by_id: &str, _alias: &str, _profile: Option<&str>, _now_iso: &str) {
        crate::s21_todo!("T15", "AliasRegistry::assign_by_id")
    }
    /// 回傳是否確實移除。
    pub fn unassign(&mut self, _alias: &str) -> bool {
        crate::s21_todo!("T15", "AliasRegistry::unassign")
    }
}
