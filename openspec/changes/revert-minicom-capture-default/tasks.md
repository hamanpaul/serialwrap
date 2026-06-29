## 1. 復原預設擷取模式

- [ ] 1.1 `sw_core/assets/tools/minicom_router.sh`：`_resolve_capture_mode()` 最終 fallback（`:76`）由 `printf 'script'` 改為 `printf 'minicom'`。以上下文鎖定，**不得**動到 `:69`（WRAPPER=1→script，6 空格縮排）與 `:71`（WRAPPER=0→minicom）。
- [ ] 1.2 以 `cat -A` 或 grep 確認改後 `:69` 仍為 `printf 'script'`、`:76` 為 `printf 'minicom'`，precedence 三層自洽。

## 2. 測試對齊

- [ ] 2.1 重寫 `tests/test_minicom_router.py::test_default_capture_uses_script_wrapper_without_minicom_capturefile` → `test_default_capture_uses_native_minicom_capturefile`：移除 `@skipUnless(script)`、fake-minicom 改為接受 `-C` 並寫入該檔、斷言 `assertIn('-C', args)` 且 BLOG_DIR 產生恰 1 個 `mini_*.log` 含 RX 內容。
- [ ] 2.2 `tests/test_minicom_router.py::test_script_unavailable_warns_and_runs_without_native_capture`：加 `env['MINICOM_CAPTURE_MODE']='script'`，維持對 script-unavailable warning 降級的覆蓋（其餘斷言不變）。
- [ ] 2.3 回歸確認（不需改）：`test_wrapper_generates_transcript_log`、`test_wrapper_prefers_home_b_log`（WRAPPER=1→script）、`test_legacy_explicit_capture_wrapper_zero`（WRAPPER=0→-C）、`test_capture_mode_minicom`、`test_capture_mode_off`、`test_invalid_capture_mode`、`test_user_capture_args_disable_auto_transcript`、`test_broker_console_detaches` 在新預設下原樣通過。
- [ ] 2.4 執行 `python3 -m pytest -q tests/test_minicom_router.py` 全綠，再 `python3 -m pytest -q tests/` 確認無新失敗（既有 flaky t1/t5/t8 與 PTY 競態除外）。

## 3. 文件與變更紀錄對齊

- [ ] 3.1 `README.md`（約 :617/:640-642）：把「預設 script」敘述改回「預設 minicom 原生 `-C`」；軟化 `minicom` 模式的「native crash 風險」措辭為中性描述（要全終端 transcript 用 `MINICOM_CAPTURE_MODE=script`）。
- [ ] 3.2 `docs/serialwrap-spec.md`（約 :580）：校正使其與 README/實作一致（復原後該句敘述反而正確，順手對齊後半句）。
- [ ] 3.3 `CHANGELOG.md` `[Unreleased]`：修正/取代既有「預設改為 script…」一條為「預設維持 minicom 原生 `-C`；`script` 為顯式 opt-in」，避免同段矛盾。
- [ ] 3.4 重生 README `serialwrap-help` marker（若受影響，R-16）。

## 4. Policy gate

- [ ] 4.1 `python3 -m policy_check --repo .` 通過（含 R-18 docs 對齊）。
- [ ] 4.2 分支 `feature/<slug>`、PR body 填 Policy Checklist、commit 帶 Co-authored-by trailer。
