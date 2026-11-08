> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

無。

## B 新發現

NO FINDINGS。

已逐筆審查 `3bb9d237`、`a5e16e17`，無 merge commit。新增 release／clear 收尾更新符合 Python 對應路徑；L1 lock 僅新增節點，未刪除案例或削弱斷言。

定向驗證（使用指定 `CARGO_TARGET_DIR`）：

- `sw-core --test drain_release`：95 passed。
- `sw-daemon --test dispatch wp58_`：4 passed。
- `git diff --check a74c8f2b..a5e16e17`：通過。

未修改檔案，工作樹維持乾淨。

## 既有碼待辦（不計入結論）

- [session_core.rs:2538](crates/sw-core/src/session_core.rs:2538)：開埠在途的 clear，若開埠最後失敗，`finish_pending_open_stop` 仍未重置 reprobe。具體情境為 DETACHED 自動重探已記 `attempts=1`，開埠期間收到 clear，隨後 `BridgeOpened(Err)`；回應仍保留 `reprobe_attempts=1` 與退避時間，Python clear 預期歸零。已核對此路徑在 `a74c8f2b` 即存在，屬靜態追查所得，未另建實測案例；建議後續補齊該分支及回歸測試。

## 對裁決的異議（不計入結論）

無。

結論：可放行