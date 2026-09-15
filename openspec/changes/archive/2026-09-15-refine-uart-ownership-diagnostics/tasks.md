## 1. 傳輸可攜性

- [x] 1.1 RED：decoder fallback／echo 假陽性／505-byte budget／過小預算。
- [x] 1.2 GREEN：雙向工具 fallback、profile budget、checksum 與 echo 安全網；regression 對齊。

## 2. Ownership

- [x] 2.1 正反例：stale ID、secondary console、reconnect grace、transfer contention、recovery epoch。
- [x] 2.2 修復重現競態並完成定向測試；未覆蓋界線列管。

## 3. CLI trace

- [x] 3.1 RED：相容性、白名單、last-attempt errno、來源一致及無額外 RPC。
- [x] 3.2 GREEN：單次解析 metadata、trace logger、全 CLI RPC 路徑及文件。

## 4. Holder 可攜性

- [x] 4.1 RED/GREEN：兩處缺 st_rdev、正常 rdev、path-match、無 /proc。

## 5. 整合交付

- [x] 5.1 Root 整合、雙語 README、完整 pytest、policy、回歸測試歸屬與成效記錄。
- [x] 5.2 審查、逐條驗證與修正後重審：最新補修兩席 Sol scoped PASS；使用者另指定 root 接替 ownership，root 最終審查 PASS，完整測試 1774 passed／16 skipped，定向 56 passed。
- [x] 5.3 OpenSpec archive 與正式 capability spec 同步（2026-09-15，4 capabilities／9 requirements）。

原 5.3 的外部交付要求維持：PR／CI／exact-head merge 驗證及 issue 結案證據
仍是整個 goal 的完成門檻。這些發生在本地封存之後，不在封存時提前勾選；
合併後以 GitHub PR 的 head／merge SHA、checks、review threads 及 issue 狀態留證。
本輪使用者已要求 root 將 goal 跑完，包含上述外部交付，不包含安裝／部署／release。

## 驗收分期（2026-09-15 確認）

本機無 UART environment；真機回歸延後至發版前有設備時執行，重大更新的穩定性驗收另在部署後執行。兩者不列為本輪 1–4 實作／本地驗證的先決條件，不因未接板取消或降低現有 pytest／policy assertions。既有及必要的實機 case 仍需保留；未跑寫 DEFERRED，不寫 PASS。

5.2 已依使用者最新指示由 root 接手完成，不把原平台中止改寫成 Sol 通過。
5.3 不代表已授權安裝、部署或操作真板。具體發版／部署證據見 `docs/refine/198-followup-validation.md`。
