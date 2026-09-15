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
- [ ] 5.2 指定審查、逐條獨立驗證、修正後重審（已批准獨立 Sol 雙審；最新補修兩席皆 scoped PASS，ownership 審查仍未完成）。
- [ ] 5.3 OpenSpec archive、PR／CI／exact-head merge 驗證及 #198 結案證據對齊。
