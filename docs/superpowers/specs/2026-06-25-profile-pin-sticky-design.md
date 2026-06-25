# 動態裝置 profile 持久化（pin + sticky）根治偵測漂移 設計（#95）

- 日期：2026-06-25
- 版本：**v2**（依 codex 對抗式審查修訂，逐項修訂摘要見 §13）
- 對應 issue：[#95](https://github.com/hamanpaul/serialwrap/issues/95)（動態偵測 session 的 profile 每次重啟重新 probe、時序敏感會漂移）
- 狀態：設計（brainstorming 產出 + codex 對抗式審查），待 user 審閱 → writing-plans。
- 真機根因已複製（§2，2026-06-25 本機 COM0 prpl→others 漂移實證）。

## 0. 已存在 / 尚未存在對照（避免誤讀為改現有行為）

本案為**全新功能**，下列符號在現行 codebase **皆不存在**，須新建：

| 符號 | 狀態 | 落點 |
|---|---|---|
| `profile_pins` / `profile_detected`（state.json 兩 map） | 新增 | `session_manager.py` load/save |
| `SessionRuntime.profile_source` 欄位 | 新增 | `session_manager.py:223` 區 / `to_public_dict`(:275) |
| `_template_by_name()` | 新增 | `session_manager.py` |
| `session.pin` / `session.unpin` RPC | 新增 | `service.py` rpc()(:600 區) |
| `session pin` / `unpin` CLI subparser | 新增 | `cli.py`(:467 subparser / :774 dispatch) |
| 四層優先序選 template | 改寫 | `_attach_by_id_dynamic`(`session_manager.py:1599`) |

現行只有：`_load_state`/`_save_state` 持久化 `aliases`/`bindings`/`released`（`session_manager.py:412`/`:450`）、`detect_template`（`login_fsm.py:138`）、`_default_passthrough_template`（`session_manager.py:1583`）。

## 1. 背景

動態偵測（無 YAML explicit target）的 session，其 profile 由 daemon 啟動時 probe 決定，**每次重啟都重新偵測**，因此會隨當下 UART 狀態漂移。

實例（2026-06-25 本機）：COM0（prplOS 板，FTDI `AQ00OAQ7`）在一次 daemon 重啟時，板子正狂吐 `wl0: dhd_flow_rings_delete_for_peer` log、prompt probe 被洗掉，偵測 fallback 到 `others-template`（passthrough），session 從 `prpl-template:COM0` 漂成 `others-template:COM0`、`command_capable:false`，agent 無法下命令。趁板子安靜 `sudo systemctl restart serialwrap` 重偵測才認回 `prpl-template` READY——但這是手動、不持久的暫時解。

## 2. 根因（code，結構性）

- **detected profile 從不持久化**：`state.json` 只存 `aliases` / `bindings`(sid→by_id) / `released`（`session_manager.py:450-451`）；偵測出的 profile/platform 不寫盤。
- **每次啟動重新 probe**：`_attach_by_id_dynamic()`(`session_manager.py:1599`) 開 PROBE bridge → `detect_template(probe_bridge, self._templates)`(`login_fsm.py:138`)。`detect_template` 行為（**v2 修正面向 7a**）：送 `\r` 收 snapshot，第一輪比對各 template `prompt_regex`（跳過 `platform=="passthrough"`，:151）命中即回；第二輪記第一個 `login_regex` candidate（:158-164）；全不符回 `None`（:169）——**它本身不回 passthrough**。fallback 由 caller 另走 `_default_passthrough_template()`(`session_manager.py:1631`)。故「正向偵測」= `prompt_regex` 命中 **或** `login_regex` candidate 命中。
- **現有 CLI 改不了既有 session 的 profile**：`session bind` 只綁 by-id（不帶 profile）、`attach`/`clear` 沿用既有 session 的 profile（`clear`=detach→re-attach 同一 session，不重選）。實測 `clear` 重探沿用 `others-template` 無效。
- **YAML `targets` explicit binding 雖可固定，但本機脆弱**：systemd-system 模式下 `/etc/serialwrap/profiles/default.yaml` 每次 `setup` 被 `sudo cp -r staging/. /etc/...` 無條件覆寫（`setup_cmd.py:370`）；且 `load_profiles`(`config.py:264`) 中 targets 只能綁同檔 template、`all_templates = ordered_templates`（覆蓋非累加，:285），獨立檔方案脆弱。

## 3. 目標 / 非目標

**目標**：core 提供「持久化動態裝置 profile」通用機制，根治重啟/重裝後漂移。採**混合機制**：手動 `pin`（權威）+ 自動 `sticky`（記住**達 READY 的**偵測），漂移**自我收斂**。

**非目標**：不改善偵測本身穩定度（屬 #69 範疇）；sticky/pin 命中後 attach/ready 失敗**不自動推翻**（靠 #69 reprobe 或人工 `unpin`）；不改 YAML `targets` 既有語意。

## 4. 設計總覽（四層優先序 + READY-gated sticky）

`_attach_by_id_dynamic(device_key)` 改為**先決定 template、再決定要不要 probe**：

| 順位 | 來源（`profile_source`） | 行為 | 寫 sticky? |
|---|---|---|---|
| 1 | `pin`（`profile_pins[device_key]`） | 命中即用，**跳過 probe** | 否 |
| 2 | `sticky`（`profile_detected[device_key]`） | 命中即用，**跳過 probe** | 否 |
| 3 | `detected`（`detect_template()` 正向命中） | 用偵測結果 | **延後到達 READY**（見下） |
| 4 | `fallback`（`_default_passthrough_template()`） | 用 others-template | 否（不鎖死，留待收斂） |

**READY-gated sticky（v2，合併面向 1+2）**：順位 3 偵測成功時**不立即寫 sticky**；改在該 session **轉入 READY** 後（`ready_probe` nonce 已驗證 = 確定正確 profile），若 `profile_source=="detected"` 才寫 `profile_detected[device_key]=profile_name`。如此：

- 杜絕 §面向 2 的 false positive——broad `prompt_regex`/`login_regex` 被 boot log 誤中、但無法達 READY 者，**永遠不會被持久化**。
- 杜絕 §面向 1 的 TOCTOU——寫盤點在 READY 後、同一把 `_lock` 內，並確認 `device_key` 對應的 `self._devices[...].real_path` 與當初 attach 的 `real_path` 一致才寫。
- **邊角**：`platform=passthrough` 的正向偵測（如 `uboot-template`，command-capable 但停 `ATTACHED` 不達 READY）→ 不寫 sticky，每次重啟重 probe（其 prompt 明確、偵測穩定，成本低）。明確記於 §9。

`pin`（順位 1）是任何情況最高權威逃生艙，不受 READY-gate 限制。

## 5. 資料模型（state.json）與 device_key

**新增兩個 device_key-keyed map**（沿用 `bindings`/`aliases` 持久化模式）：

```json
{
  "aliases": [...], "bindings": {...}, "released": {...},
  "profile_pins":     { "<device_key>": "<profile_name>" },
  "profile_detected": { "<device_key>": "<profile_name>" }
}
```

**device_key（v2，面向 4）**：不直接用「by-id」字面，而用**與既有 binding/DeviceWatcher 一致的 device key**——即 `DeviceWatcher` 掃描鍵（`device_watcher.py:43-57`，預設 `/dev/serial/by-id/...` path；同 `real_path` 只留第一個）。**同款晶片（如 CH340）by-id 相同會張冠李戴**（CLAUDE.md/README/`docs/serialwrap-spec.md` 明載須改 by-path）→ 故：

- `session pin --selector` 接受 COM/alias/sid/**by-id/by-path**，解析到該裝置當前的 DeviceWatcher device key 當 map key。
- 文件明示：多板同晶片時應以 **by-path** 綁定（與既有 binding 規範一致），避免 pin/sticky 錯置。

**持久化正確性（v2，面向 3）**：

- `__init__` **先初始化** `self._profile_pins={}` / `self._profile_detected={}`，**再** `_load_state()`（否則 :360/:377 啟動即 `_save_state()` 會以未定義/空值洗掉新 key）。
- `_save_state()` payload **同步納入**兩 map（與 `aliases`/`bindings`/`released` 並列，`sort_keys=True`）。
- 兩 map 所有讀寫**一律 `self._lock` 內**；寫 `profile_detected` 遵守既有「unlocked 等待後寫 state 要守 FLASHING/RELEASED 不被覆寫」原則（[flashing-state-integrity-hardening]）。FLASHING 為 transient 不持久化；RELEASED 照 `:441` 既有邏輯保留。
- 向後相容：舊檔無新 key → `.get(..., {})` 空 dict。

## 6. 資料流（`_attach_by_id_dynamic` 重構）

```
1. tpl, source = None, None
2. pin = self._profile_pins.get(device_key)
   if pin and _template_by_name(pin): tpl, source = that, "pin"
3. if tpl is None:
       sticky = self._profile_detected.get(device_key)
       if sticky and _template_by_name(sticky): tpl, source = that, "sticky"
4. if tpl is None:                       # 只有走到這裡才開 PROBE bridge
       probe_real_path = self._devices[device_key].real_path   # 記住，供 READY 後一致性檢查
       detected = detect_template(probe_bridge, self._templates)
       if detected: tpl, source = detected, "detected"
5. if tpl is None:
       tpl, source = self._default_passthrough_template(), "fallback"
6. 用 tpl 的 uart 參數開 bridge、建 session、session.profile_source = source
   （sticky 寫入延後到 §4 的 READY transition）
```

- pin/sticky 命中**完全不開 PROBE bridge**（省 probe、避吐 log 干擾）。
- `_template_by_name(name)`：從 `self._templates` 以 `profile_name` 查；查不到回 `None`（該順位視同未命中，往下走——robust 對抗 YAML 改名/刪除）。

## 7. CLI 介面與 RPC

```bash
serialwrap session pin   --selector COM0 --profile prpl-template   # 寫 profile_pins，最高權威
serialwrap session unpin --selector COM0                           # 只清 profile_pins，不動 sticky
```

- `pin` **驗證 `--profile` 為已載入 template**，未知 → `UNKNOWN_PROFILE`、不寫。**驗證須對 service 持有的完整 template 集合**（非單一 YAML 檔；因 `load_profiles` 的 `all_templates` 多 YAML 時被最後檔覆蓋，:285——**v2 面向 6 附注**：UNKNOWN_PROFILE 以 SessionManager 實際載入的 `self._templates` 為準）。
- selector 解析不到 device_key → 回錯。
- 對**有 YAML explicit target** 的裝置 `pin` → `PROFILE_IS_EXPLICIT`、不寫（判斷見 §8 provenance，**不**用 by-id map 反推）。
- pin/unpin 寫入後回更新後 session；**不主動重新 attach**（避免意外中斷）；對已存在的 session，**下次 daemon 重啟生效**（重啟時 session 重建走動態偵測路徑才重讀 pin/sticky）。執行期 `clear`/`attach` 沿用既有 session 的 profile、不重選（重選需變更 session_id，牽動 arbiter/alias，本案不做）。
- 清 sticky 不另做指令（YAGNI）：`pin` 正確值即覆蓋（順位 1 蓋順位 2）。
- **RPC**：`rpc()` 平面 if/elif 加 `session.pin` / `session.unpin`（不引 registry）；回應維持 `dict[str,Any]`+`ok`+失敗附 `error_code`。

## 8. provenance / 可觀測性（`SessionRuntime.profile_source`）

**v2（面向 5）**：`profile_source` 不只是顯示欄位，更是**provenance 真相來源**，一物三用（顯示 / sticky 寫入判斷 / explicit 判斷）：

| 值 | 設定時機 |
|---|---|
| `yaml-target` | `__init__` 建 YAML explicit-target session 時設（`:361` 區） |
| `pin` / `sticky` / `detected` / `fallback` | `_attach_by_id_dynamic` 依命中順位設（§6） |

- `to_public_dict()`(:275) 輸出 `profile_source`，`session list` 可見。
- **explicit 判斷**：`pin`/`unpin` 以目標 session 的 `profile_source=="yaml-target"` 判 `PROFILE_IS_EXPLICIT`——不靠「by-id 是否在某 map」反推（codebase 原無 provenance，反推不可靠）。
- **sticky 寫入判斷**：READY transition 時 `profile_source=="detected"` 才寫 `profile_detected`（§4）。

## 9. 邊界與錯誤處理

| 情境 | 處理 |
|---|---|
| 舊 `state.json`（無新 key） | `.get(..., {})` → 空 dict |
| pin profile 名不存在 | `pin` 時拒（`UNKNOWN_PROFILE`，對 `self._templates` 驗證）；attach 時 `_template_by_name` 回 None → 該順位未命中往下走 |
| 對 YAML explicit-target 裝置 pin | `PROFILE_IS_EXPLICIT`（以 `profile_source` provenance 判，§8） |
| `unpin` 後 | 清 `profile_pins[device_key]`；若 `profile_detected` 還在 → 回 sticky，否則回動態偵測（unpin 不碰 sticky） |
| 想完全回純動態（清 sticky） | 無專用指令（YAGNI）；`pin` 正確值覆蓋，或人工編 state.json。後續可加 `session redetect` |
| 同款晶片 by-id 碰撞 | device_key 改用 by-path（§5）；文件明示多板同晶片用 by-path 綁定 |
| `uboot-template` 等 passthrough 正向偵測 | 不達 READY → 不寫 sticky，每次重啟重 probe（偵測穩定、成本低） |
| sticky/detected attach 後達不到 READY | 依「不自動推翻」：停在該 profile 狀態，靠 #69 reprobe / 人工 `unpin`/`pin` 修正 |
| RELEASED / FLASHING 中 pin/unpin | 只改 state map、不觸發 attach；寫 `profile_detected` 守 FLASHING/RELEASED |
| 並發 | 兩 map 讀寫一律 `self._lock` 內 |

## 10. 改動面（顯式同步多表面，呼應 CLAUDE.md）

- `sw_core/session_manager.py`：兩 map load/save（含 `__init__` 順序）、`_attach_by_id_dynamic` 優先序重構、READY transition 寫 sticky（含 real_path 一致性檢查）、`_template_by_name()`、`SessionRuntime.profile_source`（含 `__init__` 標 yaml-target）入 `to_public_dict`。
- `sw_core/service.py`：`session.pin` / `session.unpin` RPC 分支。
- `sw_core/cli.py`：`pin` / `unpin` subparser（selector 接受 by-path）與 dispatch。
- `README.md` / `docs/**`：狀態機、session 管理、device_key/by-path 綁定規範（R-16/R-18）。
- `CHANGELOG.md`（`[Unreleased]`）；非 release chore 不動 `VERSION`（必要時 `policy-exempt:version`）。
- `tests/`（§11）。

## 11. 測試策略

**Unit**

- 四層優先序各一例（pin > sticky > detect > fallback）。
- **READY-gated sticky**：偵測成功但未達 READY（mock ready 失敗）→ **不**寫 sticky；達 READY → 寫。
- TOCTOU：READY 前 device real_path 變更 → **不**寫 sticky。
- pin/sticky 命中時 `detect_template` **未被呼叫**（mock 驗證跳過 probe）。
- `pin` 未知 profile → `UNKNOWN_PROFILE`；對 `profile_source==yaml-target` session → `PROFILE_IS_EXPLICIT`。
- `unpin` 只清 pin、保留 sticky。
- reload state 沿用 pin/sticky；向後相容載入舊 state；`__init__` 啟動 `_save_state` 不洗掉新 key。
- 寫 sticky 的 `_save_state` 在 FLASHING/RELEASED 下不破壞守護。
- device_key：同 real_path by-id 碰撞時以 by-path 為 key（mock DeviceWatcher 掃描）。

**整合（PTY 假 target，沿用 `tests/test_multiagent_e2e.py` 風格）**

- 動態裝置偵測成 prpl → 達 READY → sticky 寫入 → 重建 SessionManager 模擬重啟 → 沿用 prpl 且 `detect_template` 未被呼叫、`profile_source==sticky`。

**真機驗證（throwaway daemon，不動 prod；沿用 [mcu-flash-broker-realhw-validation]/[attach-reprobe-realhw-validation] 手法）**

- `pin COM0 prpl-template` → 重啟 throwaway daemon → COM0 仍 `prpl-template:COM0` READY、`profile_source:pin`。
- 不 pin、安靜偵測成 prpl 達 READY → 重啟 → `profile_source:sticky`、仍 prpl。

## 12. 流程與政策（CLAUDE.md）

- 分支 `feature/95-profile-pin-sticky`（已建）；R-12 僅接受 `feature/` 前綴。
- PR body 寫 `Closes #95`（R-17）。
- 跑 `pytest -q tests/` 與 `policy_check --repo .`；本地複現 CI PR 規則需帶 `--pr-title/--pr-body/--pr-base-ref/--pr-head-ref`（[policy-r12-branch-feature-only-and-local-vs-ci]）。
- 繁中 commit（Conventional Commits）+ `Co-authored-by: Copilot` trailer。

## 13. v2 修訂摘要（依 codex 對抗式審查）

| 面向 | 嚴重度 | 修訂 |
|---|---|---|
| 1 TOCTOU | HIGH | sticky 改 **READY-gated** + 寫前確認 real_path 一致（§4/§6/§11） |
| 2 false positive 不收斂 | LOW | 同上：未達 READY 不持久化（§4） |
| 3 save 未同步 | MEDIUM | `__init__` 先初始化兩 dict 再 load；`_save_state` payload 納入（§5） |
| 4 by_id key 不穩 | HIGH | 改 **device_key**（同晶片 by-id 碰撞用 by-path）；pin 接受 by-path（§5/§9） |
| 5 無 provenance | HIGH | `SessionRuntime.profile_source` 兼作 provenance，explicit 判斷不反推（§8） |
| 6 同步表面缺 + UNKNOWN_PROFILE 多 YAML | MEDIUM | §10 全表面清單；UNKNOWN_PROFILE 對 `self._templates` 驗證（§7） |
| 7 spec 事實不符 | LOW | §0 已存在/未存在對照表；§2 修正 detect 條件描述 |
