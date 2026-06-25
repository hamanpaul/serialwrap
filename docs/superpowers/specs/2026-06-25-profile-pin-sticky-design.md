# 動態裝置 profile 持久化（pin + sticky）根治偵測漂移 設計（#95）

- 日期：2026-06-25
- 對應 issue：[#95](https://github.com/hamanpaul/serialwrap/issues/95)（動態偵測 session 的 profile 每次重啟重新 probe、時序敏感會漂移）
- 狀態：設計（brainstorming 產出），待 codex 對抗式審查 → user 審閱 → writing-plans。
- 真機根因已複製（§2，2026-06-25 本機 COM0 prpl→others 漂移實證）。

## 1. 背景

動態偵測（無 YAML explicit target）的 session，其 profile 由 daemon 啟動時 probe 決定，**每次重啟都重新偵測**，因此會隨當下 UART 狀態漂移。

實例（2026-06-25 本機）：COM0（prplOS 板，FTDI `AQ00OAQ7`）在一次 daemon 重啟時，板子正狂吐 `wl0: dhd_flow_rings_delete_for_peer` log、prompt probe 被洗掉，偵測 fallback 到 `others-template`（passthrough），session 從 `prpl-template:COM0` 漂成 `others-template:COM0`、`command_capable:false`，agent 無法下命令。趁板子安靜 `sudo systemctl restart serialwrap` 重偵測才認回 `prpl-template` READY——但這是手動、不持久的暫時解。

## 2. 根因（code，結構性）

- **detected profile 從不持久化**：`state.json` 只存 `aliases` / `bindings`(sid→by_id) / `released`（`sw_core/session_manager.py:450-451`）；偵測出的 profile/platform 不寫盤。
- **每次啟動重新 probe**：`_attach_by_id_dynamic()`(`session_manager.py:1599`) 開 PROBE bridge → `detect_template(probe_bridge, self._templates)`(:1620) 比對各 template `prompt_regex`；失敗即 `_default_passthrough_template()`(:1583) fallback 到 `others-template`——**時序敏感**。
- **現有 CLI 改不了既有 session 的 profile**：`session bind` 只綁 by-id（不帶 profile）、`attach`/`clear` 沿用既有 session 的 profile（`clear`=detach→re-attach 同一 session，不重選）。實測 `clear` 重探沿用 `others-template` 無效。
- **YAML `targets` explicit binding 雖可固定，但本機脆弱**：systemd-system 模式下 `/etc/serialwrap/profiles/default.yaml` 每次 `setup` 被 `sudo cp -r staging/. /etc/...` 無條件覆寫（`setup_cmd.py:370`）；且 `load_profiles`(`config.py:264`) 中 targets 只能綁同檔 template、`all_templates = ordered_templates`（覆蓋非累加），獨立檔方案要自帶 template 定義並依賴排序，脆弱且有維護債。

## 3. 目標 / 非目標

**目標**

- 在 core 提供「持久化動態裝置 profile」的通用機制，根治重啟/重裝後的 profile 漂移；所有使用者/裝置受惠。
- 採**混合機制**：手動 `pin`（權威）+ 自動 `sticky`（記住成功偵測），漂移可**自我收斂**。

**非目標**

- 不改善偵測本身的穩定度（如 probe 重試到 RX 轉閒）——那是 #69 重探機制的範疇，本案不重做。
- sticky/pin 命中後 attach/ready 失敗**不自動推翻**（不做自動 re-detect 覆寫；靠 #69 reprobe 或人工 `unpin` 修正）。
- 不改 YAML `targets` 既有語意；本案只服務「無 explicit target 的動態裝置」。

## 4. 設計總覽（四層優先序）

`_attach_by_id_dynamic(by_id)` 改為**先決定 template、再決定要不要 probe**（現為先無條件 probe）：

| 順位 | 來源 | 行為 | 寫 sticky? |
|---|---|---|---|
| 1 | `profile_pins[by_id]` | 命中即用，**跳過 probe** | 否 |
| 2 | `profile_detected[by_id]`（sticky） | 命中即用，**跳過 probe** | 否 |
| 3 | `detect_template()` 偵測成功（非 fallback） | 用偵測結果 | **是** |
| 4 | `_default_passthrough_template()` fallback | 用 others-template | **否**（不鎖死，留待下次收斂） |

**自我收斂不變式**：sticky 只記「順位 3 的正向偵測」、絕不記順位 4 的 fallback。故初次插入若撞吐 log → fallback others（不記）→ 下次安靜時偵測成 prpl → 記住；`pin` 是任何情況的最高權威逃生艙。

## 5. 資料模型（state.json）

沿用既有 `bindings`/`aliases` 的「YAML 預設 + 執行期 override 持久化」模式，新增兩個 **by_id-keyed** map（key 用穩定的 by_id，非會漂的 sid）：

```json
{
  "aliases": [...], "bindings": {...}, "released": {...},
  "profile_pins":     { "<by_id>": "<profile_name>" },
  "profile_detected": { "<by_id>": "<profile_name>" }
}
```

- 向後相容：舊檔無此 key → `.get(..., {})` 取空 dict，無痛升級。
- 序列化沿用 `json.dumps(..., sort_keys=True)`，維持 diff 穩定（`_save_state`）。
- `_load_state` 解析這兩個 map 到 `self._profile_pins` / `self._profile_detected`（dict[str,str]，與 `_binding_overrides` 同層、同樣在 `self._lock` 保護下讀寫）。

## 6. 資料流（`_attach_by_id_dynamic` 重構）

現流程：開 PROBE bridge → `detect_template` → stop → 用正確 uart 重開。重構為：

```
1. tpl, source = None, None
2. pin = self._profile_pins.get(by_id)
   tpl, source = (_template_by_name(pin), "pin") if pin 且該 template 存在 else (None, None)
3. if tpl is None:
       sticky = self._profile_detected.get(by_id)
       tpl, source = (_template_by_name(sticky), "sticky") if sticky 且存在 else (None, None)
4. if tpl is None:              # 只有走到這裡才開 PROBE bridge
       detected = detect_template(probe_bridge, self._templates)
       if detected is not None:
           tpl, source = detected, "detected"
           self._profile_detected[by_id] = detected.profile_name   # 寫 sticky（lock 內）
           self._save_state()                                      # 守 FLASHING/RELEASED、原子
5. if tpl is None:
       tpl, source = self._default_passthrough_template(), "fallback"   # 不寫 sticky
6. 用 tpl 的 uart 參數開 bridge、建 session、記 source 供 to_public_dict
```

**關鍵**：

- pin/sticky 命中時**完全不開 PROBE bridge**（省一次 probe、避開吐 log 干擾）→ 需把「開 probe」延後到順位 4。
- `_template_by_name(name)`：新增小工具，從 `self._templates` 以 `profile_name` 查 template；查不到回 `None`（→ 該順位視為未命中，往下走，robust 對抗 YAML 改名/刪除）。
- 寫 `profile_detected` 的 `_save_state()` 須在 `self._lock` 內、且遵守既有「unlocked 等待後寫 state 要守 FLASHING/RELEASED 不被覆寫」原則（呼應 [flashing-state-integrity-hardening]）。

## 7. CLI 介面與 RPC

沿用 `session bind` 的 selector 風格（selector 解析到 by_id 當 key）：

```bash
serialwrap session pin   --selector COM0 --profile prpl-template   # 寫 profile_pins，最高權威
serialwrap session unpin --selector COM0                           # 只清 profile_pins，不動 sticky
```

- `pin` **驗證 `--profile` 是已載入 template**，未知 → `error_code: UNKNOWN_PROFILE`、不寫入。
- selector 解析不到 by_id → 回錯（沿用既有 selector 解析）。
- 對**有 YAML explicit target** 的裝置 `pin`（該 session 走固定 profile、不經 `_attach_by_id_dynamic`，pin 不生效）→ `error_code: PROFILE_IS_EXPLICIT`、不寫入（避免靜默無效）。
- pin/unpin 寫入後回傳更新後 session 狀態；**不主動重新 attach**（避免意外中斷），下次 attach/clear 或重啟生效（spec 與 CLI help 明示，避免「pin 完以為立刻變」）。
- **清 sticky 不另做指令**（YAGNI）：要修正記錯的 sticky 直接 `pin` 正確值（順位 1 永遠蓋順位 2）；要完全回到純動態偵測，`unpin` 後 sticky 仍在屬已知邊角（§9）。
- **RPC**：`SerialwrapService.rpc()` 平面 if/elif 加 `session.pin` / `session.unpin` 兩分支（沿用平面分派慣例，不引入 registry）；回應維持 `dict[str,Any]` + `ok` + 失敗附 `error_code`。

## 8. 可觀測性（`profile_source`）

`session list` 每筆（`SessionRuntime.to_public_dict`）增加 `profile_source` 欄位，讓「為什麼是這個 profile」一眼可見：

| 值 | 意義 |
|---|---|
| `pin` | 來自 `profile_pins`（手動） |
| `sticky` | 來自 `profile_detected`（記住的偵測） |
| `detected` | 本次新偵測（剛寫入 sticky） |
| `fallback` | 偵測失敗落到 others-template |
| `yaml-target` | 來自 YAML explicit target（非動態裝置） |

`source` 為 runtime 欄位（不入持久化 schema，由本次 attach 決定）。

## 9. 邊界與錯誤處理

| 情境 | 處理 |
|---|---|
| 舊 `state.json`（無新 key） | `.get(..., {})` → 空 dict，無痛升級 |
| pin profile 名不存在於 templates | `pin` 時拒絕（`UNKNOWN_PROFILE`）；attach 時若已不存在 → 該順位未命中、往下走 |
| 對 YAML explicit-target 裝置 pin | `PROFILE_IS_EXPLICIT`、不寫入 |
| `unpin` 後 | 清 `profile_pins[by_id]`；若 `profile_detected[by_id]` 還在 → 下次回 sticky，否則回動態偵測（unpin 不碰 sticky，語意單純） |
| 想完全回純動態偵測（清 sticky） | 已知邊角：無專用指令；`pin` 正確值即可覆蓋，或人工編 `state.json`。列入 issue 後續可加 `session redetect`（YAGNI，本案不做） |
| RELEASED / FLASHING 中 pin/unpin | 只改 state map、不觸發 attach，不違反交接/燒錄守護；寫 `profile_detected` 的 `_save_state` 守 FLASHING/RELEASED |
| pin 的 profile UART 參數與實際不符 | 用 pin 的 template 開 bridge（亂碼是 pin 錯的責任，依「不自動推翻」不介入） |
| 並發 | 兩 map 讀寫一律 `self._lock` 內（沿用 `_binding_overrides`） |

## 10. 改動面（顯式同步多表面，呼應 CLAUDE.md）

- `sw_core/session_manager.py`：兩 map 的 load/save、`_attach_by_id_dynamic` 優先序重構、`_template_by_name()`、`profile_source` 入 `to_public_dict`。
- `sw_core/service.py`：`session.pin` / `session.unpin` RPC 分支。
- `sw_core/cli.py`：`pin` / `unpin` subparser 與參數。
- `README.md` / `docs/**`：狀態機與 session 管理章節（R-16/R-18）。
- `CHANGELOG.md`（`[Unreleased]`）；非 release chore 不動 `VERSION`（必要時上 `policy-exempt:version`）。
- `tests/`（見 §11）。

## 11. 測試策略

**Unit**

- 四層優先序各一例（pin > sticky > detect > fallback）。
- sticky 只記正向偵測、fallback **不**寫 sticky。
- pin/sticky 命中時 `detect_template` **未被呼叫**（mock 驗證跳過 probe）。
- `pin` 未知 profile → `UNKNOWN_PROFILE`；對 explicit-target 裝置 → `PROFILE_IS_EXPLICIT`。
- `unpin` 只清 pin、保留 sticky。
- reload state 後沿用 pin/sticky；向後相容載入無新 key 的舊 state。
- 寫 sticky 的 `_save_state` 在 FLASHING/RELEASED 下不破壞守護（沿用既有 state-integrity 測試手法）。

**整合（PTY 假 target，沿用 `tests/test_multiagent_e2e.py` 風格）**

- 動態裝置偵測成 prpl → sticky 寫入 → 重建 SessionManager 模擬重啟 → 沿用 prpl 且 `detect_template` 未被呼叫。

**真機驗證（throwaway daemon，不動 prod；沿用 [mcu-flash-broker-realhw-validation] / [attach-reprobe-realhw-validation] 手法）**

- `pin COM0 prpl-template` → 重啟 throwaway daemon → COM0 仍 `prpl-template:COM0` READY、`profile_source: pin`。
- 不 pin、安靜偵測成 prpl → 重啟 → `profile_source: sticky`、仍 prpl。

## 12. 流程與政策（CLAUDE.md）

- 分支 `feature/95-profile-pin-sticky`（已建）；R-12 僅接受 `feature/` 前綴。
- PR body 寫 `Closes #95`（R-17 closing keyword）。
- 跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`；本地複現 CI 的 PR 規則需帶 `--pr-title/--pr-body/--pr-base-ref/--pr-head-ref`（呼應 [policy-r12-branch-feature-only-and-local-vs-ci]）。
- 繁中 commit（Conventional Commits）+ `Co-authored-by: Copilot` trailer。
