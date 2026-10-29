# Stage 1 出口審查 B 修正回應（S1X-b-1、S1X-b-2）

> 依據：`origin/docs/parity-amendments` 的 `reviews/s1-exit/opus-b-20261029.md`（審查 B）與 `reviews/rulings/s1.md` 的 S1X-b-1、S1X-b-2 裁決（照修）。
> 分支：`feature/s1x-b-fixes`（自 `origin/feature/rust-refactor` @ `62ffec7`）。
> 實測環境：CPython 3.12.3（`/usr/bin/python3.12`，與 `contracts/cli_help/_meta.json` 一致）＋PyYAML＋pytest＋jsonschema 4.10.3；Rust 1.96.1（rust-toolchain 釘定）；測試以一般使用者 `tester`（非 root）執行。

## 處理

| 編號 | 處理 | commit |
|---|---|---|
| S1X-b-1 | 照修：字元類別逐 token 讀取，讀到 `a-b` 當下即檢查範圍 | `1b8719a` |
| S1X-b-2 | 照修：`event add --file` 含孤立 surrogate 時，連線成功後、送出前以 INTERNAL_ERROR exit 1 拒送 | `ef924e9` |

報告的重現全部成立：

- S1X-b-1：修正前 `[z-a`、`[z-ab`、`[\d-z` 回 `unterminated character set at position 0`，`[z-a\A]` 回 `bad escape \A at position 4`；Python 3.12.3 皆為 `bad character range … at position 1`。
- S1X-b-2：修正前 Rust exit 0 並送出 `"lone":"�"`；Python exit 1、stub 連線建立但收到 0 bytes。報告的 `position 253` 對應其測試文件；本分支以 7 種最小文件逐一實測 Python 後寫入回歸（例如 `{"lone":"\udc80"}` 為 `position 52`）。

## 不成立主張

### 1. S1X-b-1 修法「錯誤位置取 `item` 的 `start`」只對單字元 token 成立

報告修法寫「錯誤位置取 `item` 的 `start`」。CPython `sre_parse._parse` 的實作是
`source.error(msg, len(this) + 1 + len(that))`，也就是「讀完 `that` 之後的位置」減去兩個 token 的**原文長度**。
tokenizer 對跳脫只取反斜線加一個字元（`\x`、`\N`），後續的 hex 或名稱由 `_class_escape` 另外讀取。
所以 token 是多字元跳脫時，訊息原文與位置都和「取 `item` 的 `start`」不同。

證據（CPython 3.12.3 `re.compile`）：

| pattern | Python 3.12.3 |
|---|---|
| `[\x7a-a]` | `bad character range \x-a at position 3` |
| `[\x7a-a` | `bad character range \x-a at position 3` |
| `[\N{LATIN SMALL LETTER Z}-a]` | `bad character range \N-a at position 23` |
| `[a-\x41]` | `bad character range a-\x at position 3` |

修正前的 Rust 對 `[\x7a-a]` 回 `bad character range \x7a-a at position 1`（第二輪檢查時的原文與起點），封閉形式也不一致。
本分支照 CPython 公式實作（`raw` 只保留反斜線加一個字元，位置＝讀完第二個 token 後的位置減去 `len(this)+1+len(that)`），
並把上表四例加進 `pyre_corpus.txt` 第六區，作為 golden 回歸。

## 範圍外的新觀察（未處理，請編排者裁決）

### pyre：樣式以單獨的 `\` 結尾時，CPython tokenizer 的預讀錯誤優先（既有碼，與 S1X-b-1 不同根因）

CPython `Tokenizer` 在 `get()` 時就預讀下一個 token；下一個 token 是結尾的單獨 `\` 時，立即拋 `bad escape (end of pattern)`，
所以這個錯誤先於「前一個 token」本身的錯誤。Rust 依序處理 token，先報前一個 token 的錯誤。修正前的 binary 同樣如此，不是本分支引入。

本分支自寫隨機差分（`[` 開頭、字元集 21 種、4 個種子 × 400 例）在修正後只剩這一類差異（6 例）。另以手寫樣式確認類別外也會發生：

| pattern | Python 3.12.3 | Rust（本分支） |
|---|---|---|
| `[z\A\` | `bad escape (end of pattern) at position 4` | `bad escape \A at position 2` |
| `a\8\` | `bad escape (end of pattern) at position 3` | `invalid group reference 8 at position 2` |
| `(?P<\` | `bad escape (end of pattern) at position 4` | `missing >, unterminated name at position 0` |
| `\A\`、`x{2\`、`a(\` | 一致 | 一致 |

影響面與 S1X-b-1 相同（`profile test` 的 `checks.<regex>.error` 文字）。修法需要在 pyre 的 token 讀取加上「結尾單獨 `\` 預讀」語意，涉及類別內外所有 token 讀取點。這超出 S1X-b-1 裁決範圍，所以本分支未處理，建議另開一條 finding 裁決。

## OWNER-BATCH

無。兩項修正都沒有改 SC／BC／DF、凍結鎖或 oracle 語意：

- `pyre.tsv` golden 只新增列，既有列逐位元組不變；產生器與 oracle（CPython 3.12.3 `re`）都沒有改。
- RPC-H-03 回應端行為不變（`pyjson::loads` 仍把孤立 surrogate 換成 U+FFFD）。

---

# 第 2 輪（sol 增量審查 `62ffec7_2a65438-sol.md`）

> 依據：`origin/docs/parity-amendments` 的 `reviews/s1-cloud/62ffec7_2a65438-sol.md`（A 段前輪 finding 全為已修；B 段 1 High＋1 Medium）、`reviews/low-backlog.md` 的 S1X-b-X1。
> 起點 `2a65438`。實測環境同上。

## 處理

| 編號 | 處理 | commit |
|---|---|---|
| S1-2a65438-1（High） | 照修：孤立 surrogate 個數隨值附帶，重複鍵覆寫時更新，只依最終值樹判定是否拒送 | `9a9955d` |
| S1-2a65438-2（Medium） | 照修：比照 CPython tokenizer 預讀，結尾單獨 `\` 的 end of pattern 優先 | 本文件所在 commit（`fix(pyre): 樣式以單獨反斜線結尾時…`） |
| S1X-b-X1（low-backlog） | 與 S1-2a65438-2 同一機制，一併修正（`[z\A\`、`a\8\`、`(?P<\` 已收進 golden） | 本文件所在 commit（`fix(pyre): 樣式以單獨反斜線結尾時…`） |

報告的重現全部成立（修正前：`{"lone":"\ud800","lone":"ok"}` Rust exit 1 拒送、Python 送出 `{"lone":"ok"}`；`[z-a\` Rust 回 `bad character range z-a at position 1`、Python 回 `bad escape (end of pattern) at position 4`）。

## 不成立主張

無。

## 範圍外的新觀察（未處理，請編排者裁決）

### pyre：與結尾反斜線無關的既有錯誤訊息差異

隨機差分（CPython 3.12.3 `re.compile` 對 Rust `pyre::compile`，種子 5～12 × 字元類別／一般各 5000 例，共 80,000 例；產生器刻意偏重錯誤構造）中，
結尾反斜線以外仍有 17,389 例不一致。這些樣式在修正前的 binary（`2a65438`）同樣不一致，不是本輪引入；本輪修正前後，不一致的樣式集合相同（無新增、無減少）。主要類別：

| 例數 | Python 3.12.3 | Rust | 例 |
|---|---|---|---|
| 11,107 | `incomplete escape \x4`／`\u00`（已讀到的跳脫原文） | `incomplete escape \x`（固定文字） | `\x4`、`[\u00[` |
| 2,688 | `unknown extension ?<\Z`、`?Px`（含完整 token） | `unknown extension ?<\`、`?P` | `(?<\Z`、`(?Px` |
| 2,120 | `missing }, unterminated name`／`missing character name` | `missing } in character name` | `\N{`、`[\N{]` |
| 440＋79 | `missing ), unterminated name`／`missing group name` | `missing ), unterminated name reference` | `(?P=` |
| 427＋76 | `missing >, unterminated name`（位置為名稱長度）／`missing group name` | 同訊息但位置 0／`missing >, unterminated name` | `(?P<\1A`、`(?P<` |
| 238 | `unexpected end of pattern` | `unknown extension ?` | `(?` |
| 151 | `bad character in group name '…'` | `unknown group name '…'` | `(?P=,` |
| 22 | `cannot refer to an open group` | `cannot refer to group defined in the same lookbehind subpattern` | `(?<=(?(1)` |

影響面同 S1X-b-1（`profile test` 的 `checks.<regex>.error` 文字；`ok`／錯誤的判定一致，僅訊息文字或位置不同）。修法分散在各跳脫與群組擴充的讀取點，超出本輪裁決範圍，建議另立 finding。

## OWNER-BATCH

無。兩項修正都沒有改 SC／BC／DF、凍結鎖或 oracle 語意；`pyre.tsv` 只新增 50 列（語料第七區 25 筆），既有列逐位元組不變，`pyre.limits.json` 不變；RPC-H-03 回應端行為不變。

---

# 第 3 輪（sol 增量審查 `2a65438_f2a23b1-sol.md`）

> 依據：`origin/docs/parity-amendments` 的 `reviews/s1-cloud/2a65438_f2a23b1-sol.md`（A 段前輪 finding 全為已修；B 段 1 High）。
> 起點 `450ecebc`（`f2a23b1` 併入 `feature/rust-refactor` 的 `897a3cd`）。實測環境：CPython 3.12.3、Rust 1.96.1，一般使用者執行。

## 處理

| 編號 | 處理 | commit |
|---|---|---|
| S1-f2a23b1-1（High） | 照修：保留模式下合法 NUL 改用無碰撞表示 `KEPT_NUL`，鍵去重、surrogate 計數與錯誤位置使用同一個鍵身分 | 本文件所在 commit（`fix(proto): 合法鍵與孤立 surrogate 鍵不再合併…`） |

報告的重現成立。Python 3.12.3 實際 CLI 搭配記錄請求行的 stub：`{"\u0000\udbb6\udc00":1,"\ud800":2}` 連線 1 次、0 筆請求行、exit 1，訊息為 `character '\ud800' in position 57`；交換順序為 position 45。修正前的 Rust CLI 對前者 exit 0，送出只剩一個鍵的請求。

- **根因**：`pyfmt` 的 surrogate 跳脫（`ESCAPE_MARK` 加偏移字元）是給 argv 用的，argv 不可能含 NUL。檔案內容的合法 NUL 後接 U+FD800..=U+FDFFF 與跳脫同形，所以字典去重把兩個鍵當成同一個。
- **修法**：`loads_keep_lone_surrogates` 解析時，合法 NUL 寫成 `KEPT_NUL`（`ESCAPE_MARK` 連寫兩次）。NUL 只會出現在這兩種形式的開頭，構成前綴碼，表示無歧義。
  - 旗標為 `false` 時，把 `KEPT_NUL` 還原成 NUL，回傳值與 `loads` 相同，送出內容不變。
  - 旗標為 `true` 時保留 `KEPT_NUL`。`event_cmd::encode_error_text` 改成以 JSON 跳脫序列為單位前進，把 `\u0000\u0000` 計為 Python 的一個 `\u0000`。
  - 原註解所記「NUL＋U+FD800 使錯誤位置提前」的已知限制一併消除。`\\` 之後接 `u0000` 的合法文字也不再被誤認。
- **回歸**（期望值都取自 Python 3.12.3 實測，修正前紅燈）：
  - `sw-cli` `event_add_document.rs::add_lone_surrogate_key_colliding_with_legal_key_is_rejected`：6 種文件，含報告反例、交換順序、值帶 surrogate、巢狀、陣列、四鍵交錯。
  - `…::add_legal_content_resembling_surrogate_escape_keeps_python_position`：5 種，位置依 Python。
  - `…::add_legal_key_resembling_surrogate_escape_is_sent`：4 種，照送並比對確切請求行。
  - `sw-proto` `pyjson_golden.rs::keep_lone_surrogates_keys_distinguish_legal_content_from_surrogates`。
  - 黑箱 `test_bb_event.py`：`test_event_add_lone_surrogate_key_colliding_with_legal_key_rejected`（3 個參數化案例），以及 `test_event_add_legal_key_resembling_surrogate_escape_is_sent`。
- **隨機差分**：以 CPython 3.12.3 的 `json.loads` 加 `json.dumps(...).encode()` 為 oracle，對 Rust CLI 實際執行並比對 rc、stdout、請求行。
  - 一般模式（種子 1～8 × 500）：修正前 1,250 例不一致，修正後 0。
  - 碰撞鍵池模式（種子 11～14 × 500）：修正前 769 例不一致，其中 98 例判定相反（應拒送卻送出）；修正後 0。

## 不成立主張

無。

## OWNER-BATCH

無。沒有改 SC／BC／DF、凍結鎖或 oracle 語意。RPC-H-03 回應端行為不變（`loads` 不使用 `KEPT_NUL`）。
