# 文件對齊 + conventions 升 1.0.5 + skill 整併/退役 MCP 設計（#67 / #59）

- 日期：2026-06-18
- 對應 issue：
  - [#67](https://github.com/hamanpaul/serialwrap/issues/67)（文件與現行架構對齊；追蹤 conventions 新版升級）
  - [#59](https://github.com/hamanpaul/serialwrap/issues/59)（serialwrap 打包成 Claude Code plugin / skill source-of-truth 收斂）
- 狀態：設計（brainstorming 產出），待 user 審閱 → writing-plans（計畫先聚焦 #67）。
- 範疇拆分結論：**#67 現在做（文件對齊 + conventions 升版，2 PR）；#59 接著做（退役 MCP + 改名 + skill 整併進 repo）**。

## 1. 背景與觸發

#67 原本是「文件對齊」issue：盤點發現 repo 文件落後多個已交付功能（event #37 / file-transfer / device-handoff #54 / command_capable #51 / human_active+soft-preempt #53 / mcu flash #55），且有文件自稱「現行完整規格」卻過時。issue 同時掛了一個「待上游發版後升級 conventions」的追蹤項。

brainstorm 期間發生兩件事改變了範疇：

1. **上游 `paulsha-conventions` 已發 v1.0.5**（lightweight tag → commit `484f963adddf384d30fa0dd85aef35dddf822ee7`），升版項從「等待」變「可執行」。
2. user 對 skills.md 那條提出三點延伸：① 以 serialwrap 這邊的 skill 為主、移除 custom-skills 那份、install 時 symlink 到 `~/.agents/skills/`；② 評估 MCP 是否還有效用；③ skill 名 `serialwrap-mcp` 會誤導、要改名。

這三點本質是 **#59（整併/打包）** 的工作，不是 #67（文件對齊）。

## 2. 調查發現（決策的事實基礎）

### 2.1 v1.0.5 只新增一條規則：R-22 `doc_reference`

比對 `policy_check/rules/` @v1.0.4 vs @v1.0.5：r01–r21 逐字相同，**唯一新增 `r22_doc_reference.py`**。

R-22 行為（讀 `r22_doc_reference.py`）：
- 掃描範圍：`README.md` + `docs/**`；排除 `openspec/`、`docs/superpowers/`、`tests/fixtures/doc-reference/`。
- 檢查：markdown 連結 `[..](path)`、反引號中「像路徑」的 token（結尾為 `.py/.md/.sh/.yml/...`）、反引號中的 `def`/`class` symbol（snake_case/CamelCase、長度 ≥3）。
- 判定：路徑在 HEAD 不存在時——若 base 存在（**本次變更刪掉**）→ **FAIL**；否則（陳年懸空）→ **WARN**；本地無 diff context → 一律降 **WARN**。symbol 被本次 diff 移除其 `def`/`class` → FAIL。
- 豁免 label：`policy-exempt:doc-reference`。
- **重點**：`openspec/` 只是不被「當作掃描範圍」，但**指向 `openspec/specs/...` 的連結 target 仍須存在**才不算懸空——正好符合 spec 降級指向 openspec 的需求。

→ R-22 正是 #67 文件工作的**守門規則**，這把「文件對齊」與「conventions 升版」兩個原本獨立的範疇耦合起來。

### 2.2 本地 policy_check 引擎是 stale 的

本地 `python3 -m policy_check --repo .` 只輸出 R-01~R-16，**不含 R-17~R-22**——目前裝的引擎比 pin 的 v1.0.4 還舊。所以先前的「全綠」是假象（從沒跑過 R-17~R-22）。要驗 R-22 必須先把引擎重裝到 v1.0.5 SHA（這本身就是升版的一部分）。

### 2.3 skills.md 與外部 skill 雙向漂移

repo `skills.md`（210 行）與 `hamanpaul/custom-skills` 的 `serialwrap-mcp/SKILL.md`（124 行、agent 執行期實際載入、本地 symlink 到 `~/.agents/skills/`）目的完全相同，但**互為非超集**：
- 外部較新：有 frontmatter、device release/attach(#54) handoff 段 + `serialwrap_release_device/attach_device`、MCU 燒錄(#55) trigger。
- repo 較全：remote support(ssh-tunnel/docker)、event trigger engine、file push/pull、完整參數規範。

且 **#59 本文「待釐清」已明列 skill source-of-truth 的決定權**（custom-skills 搬進 serialwrap / serialwrap 為主），並發現 MCP server 沒註冊。→ canonical 歸屬屬 #59，不是 #67。

### 2.4 MCP 評估：目前無淨效用，判決退役

`sw_mcp/server.py` 是「MCP adapter」之名、CLI/JSON shim 之實：

| 證據 | 內容 |
|---|---|
| 非合規 MCP | `run_stdio`（`server.py:406`）是自訂 `{"tool","params"}` JSON 行迴圈，無 `initialize`/`tools/list`/`tools/call`/`jsonrpc` → `claude mcp add` 接不起來 |
| 三 host 都沒註冊 | `claude mcp list`=空；codex `config.toml` serialwrap 區塊只有 `trust_level`、無 `[mcp_servers.*]`；copilot 無 |
| 零腳本依賴 | 全 repo + `~/.paul_tools` + custom-skills 掃過，唯一出現 `serialwrap-mcp --tool` 的地方是 server.py 自己的 `--help` 範例 |
| 與 CLI 重複 | `_TOOL_MAP` 是 `serialwrap_*` → 內部 RPC 的 1:1 別名；CLI(`sw_core.cli`)走同一 `rpc_call`、`cli.py:27` 吐同樣 compact JSON，子命令是 `_TOOL_MAP` 的超集 |

對照 MCP 的理論優勢，serialwrap 全部已被 skill / CLI / daemon 覆蓋：

| MCP 理論優勢 | serialwrap 現況 |
|---|---|
| 結構化 I/O | CLI 已吐同樣 JSON（同一 `rpc_call`） |
| 給 model 受控介面（MCP 當年存在理由）| 已由 **skill** 取代且更優（skill 能表達操作順序、安全邊界、短命令原則等 tool schema 表達不了的東西）|
| 單寫者安全仲裁 | 由 **daemon** 強制，非 MCP 層 |
| 遠端存取 | CLI 已有 `--endpoint` + ssh-tunnel |
| 跨 host 可攜 | 三 host 皆有 shell；skill 已 symlink 進共用 `~/.agents/skills/` |

成本面：`_TOOL_MAP` 須與每個新 RPC method 1:1 同步 → 每個功能要在「`_TOOL_MAP` + skill MCP tool 清單 + skill CLI 段」三處重複維護，本身就是漂移源。

**判決：退役 MCP。** 原始理由（skill 之前的受控介面）已被 skill 完全且更好取代，其餘優勢 serialwrap 早各自解決，現在只剩維護成本與誤導性的名字。

### 2.5 remote/NAT 是傳輸層問題，與 MCP 正交（最終確認）

NAT 穿透 / 跨國接入由 tunnel/relay（SSH `-R`/autossh、Tailscale/WireGuard、ngrok/Cloudflare Tunnel）解決，**不是 app 協定的事**：
- serialwrap 把 app 協定（JSON-RPC over socket/TCP）與傳輸解耦——只要一個能通到 daemon 的 TCP port，不在乎那 port 來自 SSH `-L`/`-R` 還是 overlay VPN。換 tunnel **零改動**。
- MCP 傳輸只有 stdio（本地子行程，沒解決 remote）與 HTTP（一樣要 ingress 才穿 NAT，HTTP 不自己打洞）。兩邊穿 NAT 需要的下層基建相同 → MCP 零增益。
- **唯一未來情境**：雲端/瀏覽器 agent（無法 tunnel 進 lab）→ 屆時才需要一個**合規的** MCP-over-HTTP server + 公開 ingress + auth。即使如此，穿 NAT 的仍是 ingress 不是 MCP，且今天的 shim 不堪用。serialwrap 真正缺的是 auth/TLS（現由 SSH 提供），不是 NAT 能力。→ 列為 #59 的 **non-goal / YAGNI 未來備註**。

## 3. 設計 A：整體結構（範疇拆分）

| | 範疇 | 時點 |
|---|---|---|
| **#67** | 文件對齊 + conventions 升 1.0.5（2 PR）| 現在；與整併解耦、馬上可 ship |
| **#59** | 退役 MCP + 改名 `serialwrap-mcp`→`serialwrap` + skill 整併進 repo | 接著；重活一次到位 |

**關鍵原則**：#67 不碰 canonical 歸屬與 skill 大改；skills.md 在 #67 只加 cross-ref 與過時標記。整併的 source-of-truth 決定全歸 #59，避免「#67 補 skills.md → #59 重寫」的 churn。

## 4. 設計 B：#67 細節

### 4.1 PR#1 — conventions 升 1.0.5（機械式）

1. `.paul-project.yml`：`policy_version: "1.0.4"` → `"1.0.5"`。
2. `.github/workflows/policy-check.yml`（三處）：`uses:@77a3e83…`→`@484f963a…`、`policy_engine_ref: 77a3e83…`→`484f963a…`、`policy_version: "1.0.4"`→`"1.0.5"`（R-20 要求 workflow literal 與 config 一致，無豁免）。
3. 四份 agent 檔（`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.github/copilot-instructions.md`）：`managed-by@v1.0.4`→`@v1.0.5`、`policy_version: 1.0.4`→`1.0.5`；另 CLAUDE.md body 內的 install 指令 SHA 與「policy engine pinned SHA」兩處 → `484f963a…`。四份必須同步（R-13/R-14）。
4. 本地重裝引擎到 v1.0.5 SHA（更新 install 指令），跑 `python3 -m policy_check --repo .` 確認 **R-01~R-22 全綠**。
5. `CHANGELOG.md [Unreleased]` 記一筆；`VERSION`（產品版號 `0.1.0`）**不動**。

升版**不需**額外動工的已核對項：
- **R-19（CI 跑測試）**：`tests.yml` 已 `python3 -m pytest -q tests/` → 通過。
- 升版檔案皆不在 `code_paths` → R-09 不觸發（但依 repo 自有政策仍記 CHANGELOG）。

> 待確認：實作時 `git ls-remote 'refs/tags/v1.0.5^{}'` 複核 SHA（lightweight tag，預期 commit = `484f963adddf384d30fa0dd85aef35dddf822ee7`）。

### 4.2 PR#2 — 文件對齊

- **刪 `sills.md`**（3 行轉址 stub）：pre-flight 先確認 README.md/`docs/**` 無任一連到它，否則 R-22 判 removed-this-change → FAIL。確認無引用才刪。
- **`docs/serialwrap-spec.md` → 降級（A 案）**：移除自稱「完整規格」字樣，改一頁薄概覽 + 列每個 capability 連到 `openspec/specs/<cap>/spec.md`（device-handoff / mcu-flash-broker / session-command-readiness / session-interactive / session-selftest）。每條連結 target 須真實存在（R-22 驗）。
- **`docs/plan.md` / `docs/todos.md`**：頂部加「歷史快照（截至 Phase 3）」標頭，明示不反映 #51/#53/#54/#55、不再維護。
- **`README.md`**：校對 detach/狀態機段，對齊最新 `RELEASED` / `ATTACHED`-vs-`READY` / `FLASHING` 語意。
- **`skills.md`（最小動作）**：頂部加 cross-ref——「本檔與 custom-skills 同名 skill 為兩份平行指南；source-of-truth 整併、改名與打包見 #59」+ 標記已知過時段。**不**做完整 #51/#53/#54/#55 補齊。
- 全程 R-22：removed-this-change=FAIL 必避；pre-existing dangling=WARN 可順手清或記錄。
- 依賴：PR#1 先 merge，故 PR#2 時本地引擎與 CI（reusable workflow）皆已是 v1.0.5、R-22 生效。

## 5. 設計 C：#59 細節（整併 + 退役 MCP + 改名）

1. **退役 MCP**：移除 `sw_mcp/server.py` 與 `serialwrap-mcp` shim（或留印 deprecation 的薄 stub 1~2 版）；`.paul-project.yml` 的 `code_paths` 拿掉 `sw_mcp/**` 與 `serialwrap-mcp`；確認 `tests/` 無引用；`install.sh` 不再 copy `sw_mcp`。觸及 code_paths → 需 CHANGELOG（R-09）。#59 原本「MCP tool 文件存在但 server 沒註冊」的洞因移除而消失。
2. **改名 `serialwrap-mcp` → `serialwrap`**：SKILL.md `name:` frontmatter、`~/.agents/skills/` 目錄名、CLAUDE.md / 記憶體引用一併改。
3. **skill 進 repo 設為 canonical**：`skills/serialwrap/SKILL.md`，**一次**寫出含 #51（command_capable + `PROFILE_NOT_COMMAND_CAPABLE`）、#53（human_active/soft-preempt）、#54（device release/attach）、#55（`/dev/ttyMCU` + `mcu patterns/status`）的權威版，**CLI-first**（去掉 ~30 個 `serialwrap_*` MCP tool 清單，改以 `serialwrap <subcmd>` 為主）。
4. **`install.sh`**：symlink `skills/serialwrap` → `~/.agents/skills/serialwrap`。
5. **移除 custom-skills 那份**：跨 repo 動作（`hamanpaul/custom-skills` 刪 `serialwrap-mcp/`）。
6. **Non-goal 備註（寫進 #59）**：remote/NAT/跨國由傳輸解耦 + tunnel 達成、MCP 無增益；唯一未來情境「雲端/瀏覽器 agent」屆時才寫合規 MCP-over-HTTP server，現在不做。

## 6. 驗收

- #67：`python3 -m policy_check --repo .`（v1.0.5 引擎）R-01~R-22 全綠；四份 agent 檔同步（R-13/R-14）；CLI help marker 同步（R-16）；R-20 workflow/config 版本一致；無新測試失敗。
- 不再有文件自稱「現行完整規格」卻落後功能；canonical 規格集中於 `openspec/specs/*`，其餘為概覽/歷史並明確標示。
- #59 完成後：repo 為 skill 唯一 source、custom-skills 無重複、skill 名為 `serialwrap`、`sw_mcp`/shim 退役、`install.sh` 直接 symlink 到 `~/.agents/skills/`。

## 7. 待辦 / pre-flight

- [ ] 複核 v1.0.5 commit SHA（deref tag）。
- [ ] PR#2 前掃 README.md/`docs/**` 是否有連到 `sills.md`。
- [ ] PR#1 重裝 v1.0.5 引擎後，記錄 R-22 對既有文件的 WARN 清單（決定順手清或留 #67 PR#2 處理）。

## 8. 已知既有 flaky（非本次改壞，列此免誤判）

- `tests/test_multiagent_e2e.py::...::test_five_agents_three_rounds_no_conflict`（CLAUDE.md 載明）
- `t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`（~機率性，pre-existing）
</content>
</invoke>
