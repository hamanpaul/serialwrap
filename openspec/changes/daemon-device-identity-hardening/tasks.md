## 1. TDD RED（先寫失敗測試，確認 RED 原因正確）

- [x] 1.1 寫 `test_com_rank.py`：亂序/並發 attach 一組 fake 裝置 → 斷言 COM 依 sorted `device_key`（by-id；by-path tiebreak 為排序單元，end-to-end 待 `DeviceInfo.by_path`，TODO）；現況應 RED（race/順序）
- [x] 1.2 寫 restart-rank 測試：同組裝置重建 SessionManager → COM↔by-id 不變
- [~] 1.3 寫 `session renumber` 測試（DEFERRED 至 follow-up）：renumber 已自本 PR 移除，測試一併 defer
- [x] 1.4 寫 rank 作用域測試：explicit target / bind 的 COM 不被 rank 覆寫
- [x] 1.5 寫 hotplug(a) 測試：DETACHED 空槽 + 不同 by-id 插入 → 繼承空槽（維持現有）
- [x] 1.6 寫 `test_multi_open_detect.py`：fake `/proc` 佈兩個 serialwrapd / 一外部 holder → doctor `_check_single_daemon` 與 status 欄位；單 daemon `ok=True`；無 fd 權限降級欄位正確

## 2. Capability A — startup 確定性 rank

- [x] 2.1 抽出 `device_key` 排序鍵 helper（by-id 優先、衝突 fallback by-path），對齊 `DeviceWatcher` 去重語意
- [x] 2.2 在 `SessionManager` 加「整批在線 dynamic 裝置一次排序配 COM」的同步分配（lock 內、spawn attach 前）；保留 `_next_dynamic_com` 給 runtime 單插
- [x] 2.3 收斂兩條 startup 入口（`service.py:464` `update_devices` + `:465` `bootstrap_attach`）走預配路徑
- [x] 2.4 確保 rank 僅作用 dynamic；explicit `targets`/`bind`/RELEASED 排除在 pool 外
- [x] 2.5 跑 1.1/1.2/1.4/1.5 轉綠

## 3. Capability A — session renumber（DEFERRED 至 follow-up）

> reviewer（superpowers + codex）審查後決定 defer：強制重編 active session 會弄壞 attach 時以值捕捉 `session_id` 的 bridge callback、flash state、lease reverse-link，須改以「拆 bridge → 改號 → 重 attach」另案重做。本 PR 已移除相關程式/測試/契約。

- [~] 3.1 `SessionManager.renumber_dynamic()`：單一 lock 區間內依 sorted by-id 重排 dynamic COM
- [~] 3.2 原子 remap：`_sessions` key、`_aliases`、`_binding_overrides`、arbiter worker 對應、in-flight `cmd_id`、console/interactive lease、`state.json`
- [~] 3.3 RPC `session.renumber` 分支（`service.py` 平面 dispatcher）
- [~] 3.4 CLI `session renumber` subparser + help（`cli.py`）
- [~] 3.5 跑 1.3 轉綠

## 4. Capability B — 多開偵測 + surfaces

- [x] 4.1 新增 module-level /proc 偵測 helper：掃 `/proc/*/cmdline` 找 serialwrapd、best-effort 讀 `/proc/<pid>/fd` 找 tty holder、回結構化結果含 `permission`/`unknown` 降級
- [x] 4.2 `doctor_cmd._check_single_daemon`（daemon-less，`{check,ok,detail,fix}` 同形）並接進 `run_doctor`
- [x] 4.3 `daemon status` 回應加多開/外部持有者欄位；偵測掃描走 executor offload（不阻塞 event loop）
- [x] 4.4 跑 1.6 轉綠

## 5. docs / 政策同步

- [x] 5.1 `CHANGELOG.md` `[Unreleased]` 記 #100 / #101
- [x] 5.2 `README.md`：doctor / daemon status JSON 契約段（`session renumber` 用法已 defer，不列入）
- [x] 5.3 `docs/serialwrap-spec.md`：RPC/CLI 契約對齊（R-18）
- [x] 5.4 全量 `python3 -m pytest -q tests/`（排除既有 flaky PTY 群）無新失敗
- [x] 5.5 `python3 -m policy_check --repo .` 通過

## 6. 實機驗證（throwaway daemon，不動 prod COM0/COM1）

- [ ] 6.1 throwaway daemon 起新碼 → 斷言 COM0=AC01QZT0、COM1=AQ00OAQ7；重啟數次皆同
- [ ] 6.2 usbipd 反序 attach（`detach 8-1/8-2` → `attach -w` 反序）→ COM 仍依 by-id 排序
- [~] 6.3 人為亂序後 `serialwrap session renumber` → snap 回 sorted（DEFERRED 至 follow-up，隨 renumber 一併 defer）
- [ ] 6.4 `detach 8-1`→DETACHED→`attach 8-1` 同板拿回 COM0（hotplug a）
- [ ] 6.5 刻意起第二個 serialwrapd（不同 socket）→ `doctor` 與 `daemon status` 報出多開
