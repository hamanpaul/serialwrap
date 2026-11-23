## Findings

本次為唯讀靜態審查，未修改檔案、執行測試或接觸正式設備。**草稿仍有會阻擋發布、使證據失效，以及讓不完整證據取得 PASS 的問題。**

以下引用固定 Git 物件：

- `M`＝`e50d4b3b03b2dd7f7328e05e7e70f88f4b5e0281`，草稿採用的 `origin/main`。
- `P`＝`2f19aba79d98afb7b9fd0e74046952f215a88c74`，p9a 已提交版本。
- `PA`＝`71ed5b44557ff3415c57bedd58ab7ee5f0fd0107`，裁決分支。
- `P10`＝`M:docs/rust-refactor/plans/10-stage3-canary.md`。
- `Spec`＝`M:docs/rust-refactor/serialwrap-rust-spec-final.md`。

p9a 程式尚非 main 實作；以下指出的是草稿擬移植材料中，尚未排入修正與驗收的缺口。A4、A5、A7 **確實已標為待裁決**，不能誤報為已擅自取得授權。

### S3A-1 [High] 草稿 L142、L257、L261 — L3 ingest 允許報告自行縮小必要分母

**問題：** 草稿宣稱固定分母並以「16 項 SATISFIED」驗收，但 p9a 的 `realhw-report` ingest 使用報告自身的 `meta.tiers` 過濾必要案例，沒有要求它符合出口項目的完整集合。

**出處：**

- `P10:338–339`：`S3-L3-P0P1-REMOTE-*` 要求完整穩定性集合。
- `P:tools/stage3/evidence_ingest.py:854–864`：從報告讀取 tiers，再用它計算 required。
- 同檔 `529–534`：不在所報 tiers 的案例直接排除。
- 同檔 `944–986`：只檢查縮小後的 required；其餘條件正常時可得到 PASS。

**後果：** 一份身分正確、只有 P0 且全部通過的報告，靜態程式路徑允許它滿足名為 P0＋P1＋remote 的出口；缺少 P1、remote 不會被算成 missing。這是未經 OD 的實質分母放寬。

**建議修法：** 在 M1／M2 明列修正：必要 family／case 集合由凍結的出口契約決定，報告只能證明涵蓋性。增加「P0-only 不得滿足 P0P1-REMOTE」的反例驗收，不能只驗完整報告能成功匯入。

### S3A-2 [High] 草稿 L173–176、A3 L357 — 長跑登記會改變自己剛凍結的工具樹

**問題：** 草稿要求整個 `realhw` 在 RC 前凍結，但候選開跑登記帳本就在這棵樹內。

**出處：**

- `P10:261`：L3 必須凍結 `realhw` tree。
- `M:realhw/acceptance/od09_freeze.py:40、772–814`：預設向 `realhw/acceptance/candidate_longrun_starts.ndjson` append。
- `P:tools/stage3/ab_runner.py:137–153、288–301`：第一次 B 側前執行登記，未傳外部 ledger。
- 同檔 `358–364`：緊接著重新驗證 frozen tree。
- `P:tools/stage3/ab_manifest.py:441–464、705–715`：帳本及其 lock 不在排除集合。

**後果：** A 側完成後，B 側的必要登記本身就造成 `tool tree mismatch: realhw`，候選側無法開始。若把帳本變動提交到正式發布分支，又會被 `M:scripts/rust/release_promote_guard.py:52–77` 擋住；因此「guard 只需新增 retirement JSON」也不成立。

**建議修法：** RC 前明確分離不可變工具與附加式執行紀錄，讓 registration、OD-09 檢查及 ingest 使用同一份可稽核帳本。保留歷史及凍結時序，不能靠取消登記或整棵 `realhw` 免驗解決。

### S3A-3 [High] B4 L484–489、M2 L257 — 「0.4.1 整包」不是合法的 Stage 3 A 側組合

**問題：** B4 建議直接使用已發布 0.4.1 bundle；A2／M5 則要求新建 1.0.0 CLI。Stage 3 A/B 契約要求兩側使用**完全相同 SHA 的 CLI**，只切換 daemon。

**出處：**

- `P10:906`：Stage 2.1～3 兩側 CLI implementation 與 SHA 必須相同。
- `P:tools/stage3/ab_manifest.py:392–401`：不相同即 `HARNESS_MANIFEST_INVALID`。
- `M:crates/sw-cli/src/lib.rs:57`：CLI 版本字串包含編譯期 package version。
- 草稿 L188、L346–347：要求 CLI／Cargo 等升為 1.0.0。

**後果：** 依建議直接拿 0.4.1 整包對新建的 1.0.0 整包，會在載入 manifest 時被拒絕；M2 的 0.4.1 對 0.5.x 彩排也有相同問題。

**建議修法：** 分開裁決「Python daemon／runtime 的基線來源」與「A 側完整 bundle」。明列製作、驗證共用候選 CLI＋凍結 Python daemon 的基線包，以及版本配對相容性；若要比較不同 CLI，必須另行裁決變更比較契約，不能只指定舊 release 名稱。

### S3A-4 [High] 草稿 L190–193 — Windows 穩定入口修法與 A/B 身分驗證互斥

**問題：** 將 testbed 全改成穩定 `bin\serialwrap.exe`，可以讓現行 deployed-content 找到 sidecar，卻不符合 A/B loader 的精確路徑要求。

**出處：**

- `M:realhw/deployed_content.py:71–94`：Windows 從入口旁的 `current-release.json` 找版本目錄。
- 同檔 `99–120`：穩定入口是另行驗證的複本。
- `P:tools/stage3/ab_manifest.py:313–318`、`P10:906`：`cli_exe` realpath 必須等於 bundle 內宣告的 CLI component。
- `P:tools/stage3/suites/l2_regression.py:43`、`suites/l3_realhw.py:380`：adapter 又會以 `side.cli_exe` 覆寫設定。

**後果：** 指向穩定複本會被 loader 拒絕；指向 `versions/<V>/bin` 則仍可能得到 unresolved tree。只修改 testbed/config，不能解除這個衝突。

**建議修法：** 在 M2 明列統一入口身分契約：區分執行入口與受測版本根，驗證 sidecar、入口複本 SHA、版本樹及實際啟動 component；或讓量測器安全支援直接版本路徑。不能只要求「tree 不是 null」。

### S3A-5 [High] T5B、M2、A7 — 遺漏 Linux L1.5 的大量非 func-test 必要案例

**問題：** 草稿把 adapter 缺口集中在 Windows-native，但目前 Linux L1.5 分母也不是單一 func-test adapter 能執行。

**出處：**

- 以 `M:tests/acceptance/cases.yaml` 與 `cases.lock.json` 的 `slice_2.4` 交集重算：Linux L1.5 共 **85 案**，其中 **57 案為 contract-blackbox**。
- 具體例：`M:tests/acceptance/cases.yaml:3653–3675` 的 `bc02-deferred-context-switch-clear`，來源是 pytest node，且為 required。
- `P:tools/stage3/suites/l15_functest.py:61–74`：只支援 `func-test/cases/*.yaml`、`func-test/scenarios/*.yaml`。
- 同檔 `170–174、207–212`：其他來源列 unsupported，且缺 bridge report。

**後果：** 即使補好 Windows-native，Linux L1.5 A/B 仍無法完整執行並取得有效證據。M2 的 1～2 週未納入這批 runner／報告整合工作；等 target 後才補會觸發工具身分變更與重跑。

**建議修法：** M0 產出逐案的「case → runner → 受測 artifact → report → 出口」映射，M2 明列 contract-blackbox／其他 runner 的承接方式與完整性驗收。不能為配合 adapter 刪除分母。

### S3A-6 [High] A3、M4、M5 — 發布改造漏掉會直接拒絕 Stage 3 的資產驗證器

**問題：** 草稿列了 mkbundle、pack、release workflow 與 promote guard，卻漏列 `release_assets.py` 的 Stage 2 專用驗證與 RC 建置入口。

**出處：**

- `M:scripts/rust/release_assets.py:111–114`：只接受 manifest `stage == "rust-2"`。
- 同檔 `142–166`：寫死帶外層目錄及 `versions/<V>` 的 manifest 路徑。
- 同檔 `220–239`：資產集合採精確白名單。
- `M:.github/workflows/release.yml:110–140`：現行 RC job 只驗證已上傳資產，**不建置、不上傳**。
- 同檔 `197–200、217–218`：promote 仍使用 Stage 2 安裝指南並再次呼叫資產驗證器。

**後果：** 即使 retirement verify 成功，Stage 3 archive 仍會被既有 RC／promote 流程拒絕；M5 所寫的 hosted CI RC 建置也不是現成能力。等證據完成才修 `scripts/`，又會越過 RC 後變更限制。

**建議修法：** M4 明列完整 Stage 3 發布路徑：RC 建置／上傳入口、manifest stage、archive layout、資產白名單、安裝指南與 promote 重驗；全部在 RC 前完成並演練。

### S3A-7 [High] A5 L387–392 — VOID 提案缺少不可被外部擾動遮蔽的 FAIL 優先規則

**問題：** A5 已誠實列為待裁決，但其建議只要求「預先宣告外部 USB 重新列舉＋OS 佐證」，沒有保留產品 FAIL／安全 FAIL 的優先權。裝置重新列舉只能證明事件發生，不能證明同輪產品失敗可作廢。

**出處：**

- `M:docs/rust-refactor/owner-decisions-2026-10-15.md:13`：OD-09 保留引擎 FAIL，安全事件為零不可調。
- `Spec:1702`：安全不變量不得豁免，既有失敗結果保留。
- `P10:1462–1466`：已有產品或安全失敗時，即使之後 abort／基礎設施故障，仍須判 FAIL。
- `P10:2848`：同 artifact 的 FAIL 不被後續 PASS 抵銷。

**後果：** 若同輪先發生危險重送或雙重持有，之後又有符合條件的 USB 事件，照目前 VOID 描述可能讓整輪退出 sticky-FAIL 集合，再用下一輪 PASS 發布。預先寫條件並不能使這種豁免合法。

**建議修法：** 明定安全 FAIL、已成立的產品 FAIL 優先於 VOID；限定可作廢的基礎設施失效窗口、因果證據與判定權限。改變非安全失敗的採計方式須新增明確 OD，不能只靠 terra 審查或引用 OD-110 個案。收斂前維持 A5(b)。

### S3A-8 [Medium] A4、B16 L605–616 — 第二板選項沒有處理既有單板裁決與凍結負載的衝突

**問題：** B16 把選擇縮成「補第二板」或「排除 two_boards」，但兩者都還缺關鍵契約處理。

**出處：**

- `PA:docs/rust-refactor/reviews/rulings/s2.md:542–546`：OD-102 明定當輪長跑只放 OPI 一板。
- `M:realhw/acceptance/README.md:53`：明確指出單板程序與 `lr-mixed` 兩板契約不一致，須 owner 收斂。
- `M:realhw/acceptance/longrun_acceptance.yaml:12、130–138`：唯一 engine case 是 `lr-mixed`；OD-109 凍結的 Windows 最低負載只有 COM0。
- `M:tests/acceptance/cases.yaml:1906–1922`：`lr-mixed` 本身就是 required、two_boards 案例。

**後果：** 補第二板不會自動產生第二板的凍結最低負載；排除所有 two_boards 又會排除 `lr-mixed`，卻沒有消除固定長跑出口及 engine 的義務。等 M2 看單板是 SKIP 還是 FAIL，不能解決這個規範衝突。

**建議修法：** 在 M0 要求 Stage 3 專屬拓樸裁決，明列 OD-102 的適用範圍、逐板負載與 OD-109 的處理方式。若保留單板，須明確修改哪些 Stage 3 覆蓋要求；若採兩板，須先解決凍結負載問題。不能把「排除案例」當成完整可發布方案。

### S3A-9 [Medium] M4R L260、B14 L588–594 — 彩排版本組合無法覆蓋所宣稱的驗收

**問題：** M4R 全部使用 0.5.1 正式 bundle，B14 又指定 Previous 為 0.5.1；此外，0.5.1 的 Stage 2 bundle 不能直接覆蓋 Stage 3 A/B manifest 分支。

**出處：**

- `P10:2673–2684`：D0～D8 必須驗證 Current、Previous、回到原 Current 的內容指紋，且 D7／D8 都要執行。
- `P10:906`、`P:tools/stage3/ab_manifest.py:399–402`：Stage 3 B 側必須有 Stage 3 retirement marker。
- `P:tools/stage3/evidence_ingest.py:1175–1180`：A/B report 的 stage 必須符合出口項目。

**後果：** Current＝Previous＝0.5.1 不能證明跨版本回滾；改用較早 stage 彩排，也不能證明 Stage 3 的 loader／ingest／退役包路徑可用。這些整合問題會延到 M5、正式 target 之後才首次暴露。

**建議修法：** 分開兩組：0.5.1↔0.5.0 用於 shakedown；另建可丟棄的 Stage 3 形狀候選，對 0.5.1 演練完整發布與 D0～D8 路徑。正式證據仍須由最後凍結的 RC 重跑，並把這段工作計入關鍵路徑。

### S3A-10 [Low] 草稿 L45 — 把 Windows L2 總數寫成 regression 數量

**問題：** 「L2 regression 35 案」不正確；草稿 A7 L417 的 34 案才符合目前凍結集合。

**出處：** 重算 `M:tests/acceptance/cases.lock.json` 的 `slice_2.4` 與 cases.yaml：Windows L2 是 **34 regression＋1 windows-native**；後者為 `M:tests/acceptance/cases.yaml:3033–3048` 的 `win-wal-f12-progress`。

**後果：** 兩處數字不一致，容易把安裝 TestPilot 誤當成涵蓋全部 35 案；該 native 案例仍需獨立執行與證據承接。

**建議修法：** L45 改為「34 案 regression，另有 1 案 windows-native」，沿用同一份逐案盤點表。

## 草稿正確且重要的判斷

1. **先做退役形狀 RC，再 promote 同一份 bundle 位元組，方向正確。** target 同時綁定 manifest、實際 bundle/runtime tree；不能驗收後再移除 Python 或替換內容。出處：`P10:2815–2827、3100`。
2. **install.sh、README 與工具修改必須排在 RC 前，判斷正確。** 現行 promote guard 沒有放行這些路徑。出處：`M:scripts/rust/release_promote_guard.py:52–77`。
3. **Windows gap 案例目前沒有自動退出 Stage 3 分母，判斷正確。** 不能把 Stage 0 篩選規則默默套過來。出處：`P10:259、882、906`。
4. **Canary 不是兩週掛機，也不能任意合併舊版本使用量。** 每平台 14 活躍日／5,000 submits／50 cycles、至少三種配額類型、單類型上限，以及 carryover 不含活躍日，均有依據。出處：`P10:171–181、219、374`。
5. **86400 是現行長跑下限；不能直接改 YAML、刪候選歷史或改 item ID 迴避凍結，判斷正確。** 但這不等於已有 Stage 3 的 24h／拓樸授權。出處：`M:realhw/acceptance/longrun_acceptance.yaml:12–16`、`PA:s2.md:540–546`。

## 擁有者待決事項的意見

「有條件同意」表示方向成立，條件仍須寫入計畫或裁決。

| 項目 | 意見 | 理由 |
|---|---|---|
| A1 | **同意 (b)** | 逐 Task 移植合理，但須把 S3A-1～6 列為移植驗收，不能只做介面對齊。 |
| A2 | **同意 (a)** | 符合受測物即發佈物；A 側仍須另解相同 CLI SHA 的要求。 |
| A3 | **有條件同意 (a)** | RC→promote 可保留，但須補資產驗證器、RC 建置入口、archive layout 與候選帳本處理。 |
| A4 | **有條件同意 (c)** | 並行可縮短 bench 時間，但新 OD 必須同時涵蓋 Stage 3 時長、硬體、排程及負載可比性，不能只追認排程。 |
| A5 | **不同意目前的 (a)** | 缺少安全／產品 FAIL 優先權；先採 (b)，VOID 規則收斂後再裁。 |
| A6 | **有條件同意 (d)** | 保底資源比空檔執行可估算，但須量化 quota／slot／每週產能，並避免把加入 0.5.1 的工作當成不增加總工期。 |
| A7 | **不同意目前的整批 (a)＋最小 (b)** | `gap` 是狀態標籤，不是豁免依據；須逐案列替代證據或被取代 OD，尤其 OD-08 原生 COM、OD-32 不得縮減分母及 Windows remote 範圍。 |
| A8 | **有條件同意 (a)** | docs 路徑可用，但 hosted job 必須取得全部被 index 引用的檔案；外存只能放未被必要檔案集合引用的資料，或補下載及 hash 驗證流程。 |
| B1 | **有條件同意 (b)** | 可調配額結構，不能下修 OD-10 的 14／5000／50、三種類型及比例限制。 |
| B2 | **同意建議** | Windows 工作站與 Linux 真實使用符合 OD-10；先量測是必要前置，未量測前不能把 3～6 週視為已成立。 |
| B3 | **有條件同意 (a)** | target 前補 restart 能力合理，但納入 0.5.1 只改變工作所在階段，不會自動消除實作與原生驗證工期。 |
| B4 | **不同意 (a) 的整包表述** | 可選 0.4.1 Python 為基線來源，不能直接用其整包配不同 CLI 的候選。 |
| B5 | **不同意目前的 (c)** | 程式只檢查 Stage 3 不代表其他必要證據自動豁免；應逐項列明補驗、有效沿用或新增 OD，不能以三類概括剩餘債務。 |
| B6 | **同意 (a)** | 產品 Python 退役、保留基線及測試工具合理；替代 OD-50／78 並在 RC 前同步安裝文件的要求應保留。 |
| B7 | **有條件同意** | 逐項 decision_ref 與板子授權正確；「未獲准破壞」只能導致阻擋，不能自行變成適用集合排除。 |
| B8 | **同意 (a)** | 整段預約與重跑窗口必要；須把安靜主機、USB hub、Windows 登出與另一使用者停機同意一起落實。 |
| B9 | **有條件同意 (a)** | 可以延後一般功能，但不能延後仍被 Stage 3 凍結案例要求的能力；OD-112 的「不進 0.5.1」也不等於「已准許晚於 1.0.0」。 |
| B10 | **有條件同意 (a)** | Python 程式碼凍結有依據，但 OD-112 明准許修改仍出貨的 `sw_core/assets/`，不能把整個目錄一概關閉。 |
| B11 | **有條件同意 (a)** | 獨立審查應先於追認，且須證明危險持有者情境仍有嚴格、決定性的驗證。 |
| B12 | **同意 (a)** | 原生、add-only、定期收集並在 target 前最後凍結，能降低後期才發現 Windows 集合缺漏的風險。 |
| B13 | **有條件同意 (a)** | 路由可採用，但模型名稱與審查次數不是驗收證據；須涵蓋分母反例、凍結副作用與完整發布路徑。 |
| B14 | **不同意目前完整建議** | 正式 Previous＝0.5.1 合理；M4R 的 Current 也為 0.5.1 則不成立，須分開彩排與正式組合。 |
| B15 | **有條件同意 (a)** | 應提供 remote 拓樸；Linux docker／SSH 對端不能替代 Windows-native OpenSSH／remote 的平台證據。 |
| B16 | **不同意等 M2 再二選一** | 單板、兩板、OD-109 負載與 `lr-mixed` 義務須先收斂；實跑得到 SKIP 或 FAIL 不會替擁有者解決契約衝突。 |

結論：需修訂後再審