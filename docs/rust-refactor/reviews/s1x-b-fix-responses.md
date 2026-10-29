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
