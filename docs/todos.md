# serialwrap — 待辦清單

## ✅ 已完成

### Phase 3: Auto-detect template + 動態 session

- [x] `config.py`：`load_profiles()` 回傳 `LoadResult`（含 templates + max_sessions）
- [x] `config.py`：`_load_templates()` 回傳排序後的 `list[ProfileTemplate]`（passthrough 排最後）
- [x] `config.py`：targets 區段變為可選（省略 → 全走動態偵測）
- [x] `login_fsm.py`：新增 `detect_template()` 函式
- [x] `session_manager.py`：新增 `_next_dynamic_com()`、`_session_from_template()`
- [x] `session_manager.py`：`_attach_by_id()` 新增動態偵測路徑
- [x] `session_manager.py`：`_attach_by_id_dynamic()` 完整實作
- [x] `service.py`：傳遞 `templates` + `max_sessions` 給 SessionManager
- [x] `serialwrapd.py`：解包 `LoadResult` 並傳入 service
- [x] `profiles/default.yaml`：加回 prpl-template、targets 改為可選、加 `max_sessions: 16`
- [x] `tests/test_login_fsm.py`：新增 7 個 `detect_template` 測試
- [x] `tests/test_session_bind.py`：新增 5 個動態 session 測試
- [x] `README.md`：更新 Profile 段落，反映動態偵測
- [x] `docs/serialwrap-spec.md`：新增 §13.5 自動偵測規格
- [x] `docs/plan.md`：新建
- [x] `docs/todos.md`：新建

## 📋 待辦

（目前無待辦項目）

## 🚫 已知限制 / Blocked

- 裝置剛通電可能在 bootloader，首次 probe 可能失敗；需依賴 recover 機制二次嘗試
- `shell` 的 `.*[$#] $` 可能與 `bcm` 的 `[>#]\s*$` 重疊；順序決定優先級（specific 排前）
