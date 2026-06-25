## 1. 持久化基礎（state.json 兩 map + provenance 欄位）

- [ ] 1.1 `SessionRuntime` 新增 `profile_source` 欄位，並在 `to_public_dict` 輸出
- [ ] 1.2 `SessionManager.__init__` 先初始化 `self._profile_pins={}` / `self._profile_detected={}`，再 `_load_state`
- [ ] 1.3 `_load_state` 解析 `profile_pins`/`profile_detected`（缺 key → 空 map），`_save_state` payload 同步納入（`sort_keys`、`_lock` 內）
- [ ] 1.4 測試：向後相容載入無新 key 的舊 `state.json`；跨重啟保留；`__init__` 首次 `_save_state` 不洗掉新 key

## 2. 四層優先序解析（_attach_by_id_dynamic 重構）

- [ ] 2.1 測試（RED）：pin > sticky > detect > fallback 各一例；pin/sticky 命中時 `detect_template` 未被呼叫（mock 驗證跳過 probe）；pin/sticky 指向不存在 template → 往下順位
- [ ] 2.2 新增 `_template_by_name()`；重構 `_attach_by_id_dynamic`：先查 pin→sticky 決定 tpl（命中跳過 PROBE bridge），未命中才開 probe 跑 `detect_template`，全不符 fallback；設定 `session.profile_source`

## 3. READY-gated sticky 寫入

- [ ] 3.1 測試（RED）：`detected` session 達 READY 才寫 sticky；未達 READY / fallback / passthrough 不寫；寫前 `real_path` 與 attach 時不一致（TOCTOU）不寫
- [ ] 3.2 在 session 轉入 READY 的路徑：`profile_source==detected` 且 `real_path` 一致時寫 `profile_detected`（`_lock` 內、守 FLASHING/RELEASED 不被覆寫）

## 4. yaml-target provenance

- [ ] 4.1 測試（RED）：YAML explicit-target session 的 `profile_source` 為 `yaml-target`
- [ ] 4.2 `__init__` 建 YAML target session 時標 `profile_source="yaml-target"`

## 5. CLI + RPC（pin / unpin）

- [ ] 5.1 測試（RED）：`pin` 有效 profile 寫入；未知 → `UNKNOWN_PROFILE`；對 `yaml-target` 裝置 → `PROFILE_IS_EXPLICIT`；`unpin` 只清 pin、保留 sticky
- [ ] 5.2 `service.rpc()` 加 `session.pin` / `session.unpin` 平面分支（UNKNOWN_PROFILE 對 `self._templates` 驗證、PROFILE_IS_EXPLICIT 以 provenance 判、device_key 解析）
- [ ] 5.3 `cli.py` 加 `session pin` / `unpin` subparser（selector 接受 by-path）與 dispatch

## 6. device_key 穩定性

- [ ] 6.1 測試（RED）：同 `real_path` 但 by-id 碰撞時以 by-path 為 device_key，pin/sticky 不張冠李戴
- [ ] 6.2 device_key 解析：`pin` selector 接受 by-path → device_key；文件化同晶片用 by-path

## 7. 整合驗證與文件同步

- [ ] 7.1 整合測試（PTY 假 target，沿用 `test_multiagent_e2e` 風格）：偵測成 prpl → 達 READY → sticky 寫入 → 重建 SessionManager 模擬重啟 → 沿用、`detect_template` 未被呼叫、`profile_source==sticky`
- [ ] 7.2 真機驗證（throwaway daemon，不動 prod）：`pin COM0 prpl-template` 重啟仍 `prpl-template:COM0` READY `profile_source:pin`；不 pin 安靜偵測達 READY 重啟 `profile_source:sticky`
- [ ] 7.3 更新 `README.md` / `docs/**`：session 管理、device_key/by-path 綁定規範、`profile_source`、`pin`/`unpin`
- [ ] 7.4 更新 `CHANGELOG.md`（`[Unreleased]`）
- [ ] 7.5 跑 `pytest -q tests/` 與 `policy_check --repo .`（帶 `--pr-title/--pr-body/--pr-base-ref/--pr-head-ref` 複現 CI），確認無新失敗
