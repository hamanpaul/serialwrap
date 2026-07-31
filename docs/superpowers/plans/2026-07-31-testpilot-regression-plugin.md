# serialwrap_regression testpilot plugin 實作計畫（#155）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本案實際派工方式（裁示）：Task 1–8 由主 agent 實作（契約敏感）；Task 9–18（family cases）由 cortex 派 agy `gemini-3.6-flash-high` 實作（fallback `claude --model haiku`）、`cg --effort high` review、主 agent 整合。

**Goal:** 把 serialwrap 已修好的實機-only bug 固化成 testpilot 回歸 plugin（10 family、約 30 case），常跑防回歸。

**Architecture:** 與 `reliability/` 平行的第二個薄殼 plugin：自有 case registry（含 family/issues），重用 realhw 的 drivers/preflight/benchlock；testpilot 為殼（分診 FailTest/FailEnv/FailConfig）；破壞性 case 受 `allow_destructive` gate；U-Boot 唯讀護欄由 harness 強制。

**Tech Stack:** Python 3.10+、hatchling、testpilot-core PluginBase（api 1.1）、pytest；驅動一律經部署版 `serialwrap` CLI subprocess（禁 import `sw_core`）。

## Global Constraints

- 全部檔案繁體中文註解/docstring；`from __future__ import annotations`；完整型別標註。
- **禁 import `sw_core`**（測部署後系統）；所有操作經 pinned CLI（testbed `serialwrap_exe`，預設 `~/.local/bin/serialwrap`），**禁裸 PATH 解析**。
- 版控檔案內**禁止字面 `/home/<user>` 絕對路徑**（R-21）；用 `~` 或相對路徑。
- testpilot 三契約陷阱（不可違反）：agent-config remediation `enabled: true`＋`max_attempts: 1`＋`hooks.enabled_hooks: [on_failure]`、不覆寫 decision hooks；`retry.max_attempts: 1`；`execute_step` 恆回 `success=True`、判決集中 `evaluate`。
- FAIL 必帶 `category`（`test`/`environment`/`configuration`）＋`reason_code`；能力缺失一律 SKIP 非 FAIL。
- oracle 原則：**當初 issue 的錯誤行為不得再現**；每 case `issues` 至少掛一個已修 issue。
- U-Boot 情境唯讀（白名單強制）；F9 case 收尾必等 READY。
- 實機命令用無副作用命令（`echo`/`true`）；submit 後隔拍再讀 status（line race）；雙板序列化送（foreground busy）。
- 每 task 完成即 commit（繁中 Conventional Commits＋`Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer）。
- 已知 flaky（非本案破壞）：`test_multiagent_e2e::test_five_agents_three_rounds_no_conflict`、`test_human_agent_coexist` 的 t1/t2/t8。

**既有事實（勿再考古）：** error codes `CMD_TOO_LONG`/`CMD_CONTAINS_NEWLINE`/`CMD_LENGTH_WARNING`/`CMD_NOT_FOUND`/`SESSION_QUEUE_FULL`/`CREDENTIALS_UNRESOLVED`（`sw_core/arbiter.py`、`sw_core/constants.py`）；`daemon status` 回 `limits.max_submit_cmd_bytes`/`warn_submit_cmd_bytes` 與 `multi_open`/`foreign_holders`；CLI 有 `session activity`/`console-list`/`interactive-*`/`cmd result-tail`/`log tail-raw|tail-text`/`file push|pull`/`device release`；0.2.4 失敗時 stderr 有 `serialwrap: <method> failed: <CODE>` 一行。bench：COM0=prpl（BGW720、U-Boot 2024.04、autoboot 3s）、COM1=bcm；COM1 U-Boot 具備性未確認（case 內偵測、無則 SKIP）。

---

### Task 1: realhw SwCli 注入 exe 路徑

**Files:**
- Modify: `realhw/drivers.py`（`SwCli` 類）
- Test: `tests/test_realhw_drivers.py`（追加）

**Interfaces:**
- Produces: `SwCli.__init__(self, exe: str = "serialwrap")`；`run()` 改用 `self._exe`。既有呼叫端（`SwCli()`）行為不變。

- [ ] **Step 1: 失敗測試**

```python
def test_swcli_exe_injectable(monkeypatch):
    captured = {}
    def fake_run(argv, timeout=30.0):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
    monkeypatch.setattr(drivers, "_run", fake_run)
    drivers.SwCli(exe="/opt/bin/serialwrap").run("doctor")
    assert captured["argv"][0] == "/opt/bin/serialwrap"
    drivers.SwCli().run("doctor")
    assert captured["argv"][0] == "serialwrap"
```

- [ ] **Step 2: 跑測試確認 FAIL**（`python3 -m pytest -q tests/test_realhw_drivers.py -k exe_injectable`，預期 TypeError：SwCli 不收 exe）
- [ ] **Step 3: 實作**

```python
class SwCli:
    """已安裝 serialwrap CLI 的薄包裝；stdout 嘗試 JSON 解析。exe 可注入（#154 pin 防線）。"""

    def __init__(self, exe: str = "serialwrap") -> None:
        self._exe = exe

    def run(self, *args: str, timeout: float = 30.0) -> dict:
        cp = _run([self._exe, *args], timeout=timeout)
        ...
```

- [ ] **Step 4: 跑 `python3 -m pytest -q tests/test_realhw_drivers.py` 全綠**
- [ ] **Step 5: Commit**（`feat(realhw): SwCli 支援注入 serialwrap 執行檔路徑`）

### Task 2: package 骨架

**Files:**
- Create: `regression/pyproject.toml`、`regression/serialwrap_regression/__init__.py`

**Interfaces:**
- Produces: dist `serialwrap-regression`、entry-point `testpilot.plugins` → `serialwrap_regression.plugin:Plugin`。

- [ ] **Step 1: pyproject.toml**（照 `reliability/pyproject.toml` 模式，name/entry-point 換為 regression；packages `["serialwrap_regression"]`）
- [ ] **Step 2: `__init__.py`**（docstring：定位一句話＋`__version__ = "0.1.0"`）
- [ ] **Step 3: Commit**

### Task 3: harness.py（Case 模型＋registry）

**Files:**
- Create: `regression/serialwrap_regression/harness.py`
- Test: `tests/test_regression_harness.py`

**Interfaces:**
- Produces:

```python
from realhw.harness import CaseResult  # 直接重用

@dataclasses.dataclass(frozen=True)
class Case:
    id: str                 # 例 "f3-fail-error-code"
    family: str             # "F1".."F10"
    title: str
    run: Callable[[Any], CaseResult]
    issues: tuple[str, ...] # 例 ("#94",)；至少一個
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

REGISTRY: list[Case] = []           # 本 package 自有，與 realhw.REGISTRY 無關
def register(case: Case) -> Case    # id 重複或 issues 空 → ValueError
def load_registry() -> list[Case]   # import cases.f01..f10 後回傳 list(REGISTRY)
FAMILY_ORDER = ("F3","F1","F5","F6","F2","F4","F7","F8","F9","F10")
def ordered(registry: list[Case]) -> list[Case]  # 依 FAMILY_ORDER 再依 id 排序
```

- [ ] **Step 1: 失敗測試**（register 拒重複 id、拒空 issues；ordered 依 FAMILY_ORDER；`load_registry()` 與 `realhw.harness.REGISTRY` 交集為空）
- [ ] **Step 2: 確認 FAIL → Step 3: 實作 → Step 4: 全綠 → Step 5: Commit**

### Task 4: ctx.py（RegCtx）

**Files:**
- Create: `regression/serialwrap_regression/ctx.py`
- Test: 併入 `tests/test_regression_harness.py`

**Interfaces:**
- Produces（case 的唯一依賴面，鏡射 realhw ctx 慣用法）:

```python
@dataclasses.dataclass
class RegCtx:
    cfg: dict[str, Any]          # testbed 載入結果：boards/serialwrap_exe/allow_destructive/timeouts
    sw: drivers.SwCli            # SwCli(exe=cfg["serialwrap_exe"])
    tmux: drivers.TmuxCtl        # prefix "swreg"
    report_dir: Path
    def note(self, name: str, content: str) -> str   # 寫 report_dir/<case>/<name>，回傳路徑字串
    def board(self, com: str) -> dict                # cfg["boards"] 依 com 查
def build_ctx(cfg: dict, report_dir: Path) -> RegCtx
```

- [ ] Steps：測試（note 落檔、SwCli 綁 pinned exe）→ 實作 → 綠 → Commit

### Task 5: guards.py（U-Boot 護欄＋ensure_ready＋ThrowawayDaemon）

**Files:**
- Create: `regression/serialwrap_regression/guards.py`
- Test: `tests/test_regression_guards.py`

**Interfaces:**
- Produces:

```python
class UBootGuardError(RuntimeError): ...

UBOOT_RO_WHITELIST = ("printenv", "bdinfo", "version", "help", "echo")
_FORBIDDEN = re.compile(r"\b(saveenv|env\s+save|env\s+default|setenv|sf\s+write|nand\s+write|mmc\s+write|tftpboot)\b")

def validate_uboot_cmd(cmd: str) -> None
    # 首 token 不在白名單 → raise；整串命中 _FORBIDDEN → raise；含分號/換行 → raise（防串接繞過）

class UBootConsole:
    """經 tmux 內 serialwrap-minicom console 與 U-Boot 互動；只暴露唯讀操作。"""
    def __init__(self, ctx, com: str, tmux_session: str) -> None
    def interrupt_autoboot(self, window_s: float = 15.0) -> bool   # 週期送鍵＋capture 至見 U-Boot prompt
    def readonly_cmd(self, cmd: str) -> str                        # validate → send → capture 回傳
    def leave(self, via: str = "boot") -> None                     # via ∈ {"boot","reset"}，其他 raise

def ensure_ready(ctx, com: str, *, timeout_s: float, recover: bool = True) -> bool
    # wait_state READY；逾時且 recover → session attach --selector 再等一輪；回最終布林

def throwaway_env(workdir: Path, by_id_dir: Path) -> dict[str, str]
    # 覆寫 SERIALWRAP_RUN_DIR/_STATE_DIR/_WAL_DIR/_BY_ID_DIR/_CONFIG_DIR/_PROFILE_DIR → workdir 下子目錄；純函式

class ThrowawayDaemon:
    """throwaway serialwrapd context manager：獨立 XDG/socket/by-id sandbox；prod 零接觸。"""
    def __init__(self, exe: str, workdir: Path, by_id_dir: Path, profile_yaml: str) -> None
    def __enter__(self) -> "ThrowawayDaemon"   # 寫 profile、nohup 啟動（純 nohup &，exit 144 坑）、等 socket
    def run(self, *args: str, timeout: float = 30.0) -> dict       # 以 throwaway env 呼叫 pinned exe
    def __exit__(self, *exc) -> None           # kill daemon、不刪 workdir（留 evidence）
```

- [ ] **Step 1: 失敗測試**——逐禁令釘死：

```python
@pytest.mark.parametrize("cmd", [
    "saveenv", "env save", "env default -a", "setenv bootdelay 0",
    "setenv bootcmd boot", "sf write 0 0 100", "nand write 0 0 100",
    "mmc write 0 0 100", "tftpboot 0x80000 fw.bin",
    "printenv; saveenv", "printenv\nsaveenv"])
def test_uboot_forbidden(cmd):
    with pytest.raises(guards.UBootGuardError):
        guards.validate_uboot_cmd(cmd)

@pytest.mark.parametrize("cmd", ["printenv", "printenv bootcmd", "bdinfo", "version", "help", "echo hi"])
def test_uboot_allowed(cmd):
    guards.validate_uboot_cmd(cmd)  # 不 raise
```

另測：`throwaway_env` 覆寫齊 6 個變數且都在 workdir 下；`leave(via="poweroff")` raise。
- [ ] Steps 2–5：FAIL → 實作 → 綠 → Commit

### Task 6: preflight.py（版本 gate＋重用）

**Files:**
- Create: `regression/serialwrap_regression/preflight.py`
- Test: `tests/test_regression_preflight.py`

**Interfaces:**
- Produces:

```python
def version_gate(cli_version: str, daemon_version: str) -> str | None
    # 以 realhw.preflight.parse_version 解析；任一解析失敗或不相等 → 問題字串；相等 → None
def stale_client_note(path_version: str, pinned_version: str) -> str | None
    # 不等 → "警告：PATH 上 serialwrap=X.Y.Z 與 pinned Z 不一致（不擋，#154 診斷）"
def run_preflight(cfg: dict) -> dict
    # 回 {ok, problems, benchlock_fd, deployed_version, notes}
    # 組合：realhw.preflight.collect（usbipd 檢查以 cfg 無鍵時跳過→tools 檢查僅 tmux/minicom/sudo）
    #  ＋acquire_benchlock（realhw bench_lock_path）＋version_gate（pinned --version vs daemon status 版本）
    #  ＋stale_client_note（shutil.which("serialwrap") 的版本 vs pinned）
```

注意（跨-plan 簽章教訓）：`benchlock_ok` 必須把 `acquire_benchlock` 結果**注入** `collect(..., benchlock_ok=...)`，不得留預設 True。
- [ ] Steps：純函式測試（gate 齊/不齊/解析失敗；note 邏輯）→ FAIL → 實作 → 綠 → Commit

### Task 7: core.py（載入、blackbox、分診、skip）

**Files:**
- Create: `regression/serialwrap_regression/core.py`
- Test: 併 `tests/test_regression_harness.py`（build_case_dicts/runtime_skip 純邏輯）

**Interfaces:**
- Produces（鏡射 reliability core 介面，plugin.py 消費）:

```python
def build_case_dicts(registry: list[Case], cfg: dict) -> list[dict]
    # 每 case → {"id", "name": title, "metadata": {family, issues, destructive, requires},
    #            "steps": [{"id": "run", "action": "run_case"}]}；依 harness.ordered 排序
def runtime_skip(meta: dict, missing_caps: dict, broken_by: str | None, allow_destructive: bool) -> tuple[str, str] | None
    # destructive 且未 allow → ("destructive_gated", "..."); requires 缺 → 對應 reason_code；broken_by → ("bench_broken_by", ...)
def run_case_blackbox(case_id: str, ctx) -> CaseResult   # 查 registry、跑 run(ctx)、例外 → FAIL(category="test", reason_code="case_exception")
def make_skip_result(reason_code: str, comment: str) -> CaseResult
def result_to_dict(r: CaseResult) -> dict
def failure_payload(d: dict) -> dict | None   # PASS/SKIP → None；FAIL → {category, reason_code, comment, evidence}
def recover_boards(ctx, coms: list[str]) -> list[str]   # 逐板 ensure_ready；回未恢復清單
```

- [ ] Steps：測試（dicts 結構、skip 判定矩陣、failure_payload 空 category 保護）→ 實作 → 綠 → Commit

### Task 8: plugin.py＋reporter＋組態檔

**Files:**
- Create: `regression/serialwrap_regression/plugin.py`、`reporter.py`、`agent-config.yaml`、`testbed.yaml.example`
- Test: `tests/test_regression_pluginfiles.py`

**Interfaces:**
- Produces：`Plugin(PluginBase)`，`name="serialwrap_regression"`、`api_version="1.1"`；生命週期照 `reliability/serialwrap_reliability/plugin.py` 模式（無 longrun 分支）：`prepare_run` 跑 preflight（不 ok → raise PreflightRefused）、`execute_step` 恆 success＋`captured["regression"]=result_to_dict`、`evaluate` 取 captured 判決、`teardown` recover_boards＋`_broken_by`、`verify_install` 檢 pinned exe／tmux／minicom／testbed／registry 數量。
- `testbed.yaml.example` 鍵：`serialwrap_exe: ~/.local/bin/serialwrap`、`allow_destructive: false`、`boards: [{com: COM0, alias: dut-prpl, serial: AC01QZT0, platform: prpl}, {com: COM1, alias: com1-brcm, serial: AQ00OAQ7, platform: bcm}]`、`timeouts: {ready_wait_s: 180, boot_wait_s: 240, cmd_timeout_s: 12}`、`tmux_prefix: swreg`。
- `agent-config.yaml`：照 reliability 同檔改 plugin 名（remediation enabled true／max_attempts 1／on_failure；retry.max_attempts 1）。

- [ ] Steps：pluginfiles 測試（檔案存在、agent-config 契約鍵值斷言、testbed 可載入、entry-point 指向可 import）→ 實作 → 綠 → Commit

---

## Family case 檔（Task 9–18）

**共通規格**（每個 task 相同，不再重複）：
- 檔案 `regression/serialwrap_regression/cases/fXX_<slug>.py`；模組頂 `_case()` decorator 照 `realhw/cases/p0.py` 模式包 `harness.register`（帶 `family=`、`issues=`）。
- case 函式簽名 `def f(ctx: RegCtx) -> CaseResult`；所有觀測落 `ctx.note()` evidence。
- 測試步驟：加完 case 後跑 `python3 -m pytest -q tests/test_regression_harness.py`（registry 載入含新 family、id 唯一）＋`python3 -c "from serialwrap_regression.harness import load_registry; print(len(load_registry()))"`。
- 不確定的 CLI 欄位名先 `serialwrap <sub> --help` 與單次手動呼叫確認再寫斷言（把輸出貼進 PR note）。
- 完成即 commit：`feat(regression): FX <family 名> cases`。

**範例（F3 完整寫法，各 family 照此 pattern）：**

```python
@_case("f3-fail-error-code", "失敗 CLI 必有非空 error_code＋stderr", family="F3", issues=("#94",))
def f3_fail_error_code(ctx):
    r = ctx.sw.run("session", "attach", "--selector", "NOSUCH")
    ctx.note("attach-nosuch.json", str(r))
    if r["_rc"] == 0:
        return CaseResult("FAIL", reason="不存在 selector 竟回成功", category="test", reason_code="error_not_reported")
    if not (r.get("error_code") or "").strip():
        return CaseResult("FAIL", reason="stdout JSON error_code 為空（#94 回歸）", category="test", reason_code="empty_error_code")
    if "failed" not in (r.get("_stderr") or ""):
        return CaseResult("FAIL", reason="stderr 無具體錯誤行（#94 回歸）", category="test", reason_code="empty_stderr")
    return CaseResult("PASS")
```

### Task 9: F3 失敗可觀測性（`cases/f03_observability.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f3-fail-error-code | #94 | 如上範例 |
| f3-cmd-fail-observable | #94 | `cmd submit --selector NOSUCH --cmd 'echo x'` → rc≠0＋error_code 非空＋stderr 有行 |
| f3-device-error-names-selector | #16 | 對不存在 selector 的錯誤訊息（stderr＋JSON）必含該 selector 字串 |
| f3-log-tail-latest | #124 | COM0 `submit_and_wait "echo MARKER_<rand>"` 後 `log tail-text --selector COM0`（預設參數）輸出必含新 marker（不得從最舊 seq 起算漏掉新段） |

### Task 10: F1 命令契約（`cases/f01_cmd_contract.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f1-limits-queryable | #129 #27 | `daemon status` 的 `limits.max_submit_cmd_bytes`>0 且 `warn_submit_cmd_bytes`>0 |
| f1-too-long-rejected | #23 #27 | 讀 limits，送長度=max+100 的 `echo ...` → error_code==`CMD_TOO_LONG`；隨後 `submit_and_wait "echo ok"` 正常（未卡死） |
| f1-newline-rejected | #27 | `cmd submit --cmd $'echo a\necho b'` → `CMD_CONTAINS_NEWLINE` |
| f1-near-limit-no-logout | #19 | 送長度=warn 門檻±0 的合法 `echo` 長串 → 執行成功；隨後 session 仍 READY＋`submit_and_wait "echo alive"` 成功（未登出） |
| f1-cmdid-survives-timeout | #15 | submit `sleep 20`＋`--cmd-timeout 3` → 等 timeout 後 `cmd status --cmd-id` 仍查得到（status∈{timeout,error,done}，非 `CMD_NOT_FOUND`） |

### Task 11: F5 console 共存（`cases/f05_console_coexist.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f5-raw-ownership-survives-agent-rounds | #78 | tmux 開 `serialwrap-minicom COM0`、確認 `console-list` 有 interactive_owner → 連續 5 輪 `submit_and_wait "echo r<i>"`（每輪 suspend/resume）→ 再驗 interactive_owner 仍在＋Tab 補完仍動作（照 realhw p0-console-raw 手法） |
| f5-deferred-bytes-flushed | #78 | agent 命令執行中（背景 submit `sleep 3`）經 tmux 送 `ec`（不含 Enter）→ 命令結束後送 Tab → pane 出現 `echo`（deferred buffer 未丟鍵） |
| f5-console-peer-gone-recycled | #53 #11 | 開 console 後 `tmux kill-session`（模擬對端消失）→ 等 10s → `console-list` 該 console 消失；再開新 console 可取得 ownership（無假性佔用） |
| f5-second-console-linebuffer | #7 #8 | 第一 console 持 ownership 時開第二個 → `console-list` 第二個非 interactive_owner；agent `submit_and_wait` 照常成功（不被 `SESSION_INTERACTIVE_BUSY` 卡） |
收尾一律 kill tmux＋sleep 3（router 清理）。

### Task 12: F6 RPC 不凍結（`cases/f06_rpc_liveness.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f6-ping-during-long-op | #80 | COM0 背景 submit `sleep 8`（`--mode background` 若有；否則前景 thread 送）→ 期間每 0.5s `daemon status` 計時：每次 RPC 往返 <2s（loop 未被凍結）；evidence 記最大延遲 |
| f6-two-boards-no-starvation | #80 #52 | 執行緒 A 對 COM0 submit `sleep 5`、同時 B 對 COM1 `submit_and_wait "echo fast"`（開始後 1s 送）→ B 於 sleep 結束前完成 |

### Task 13: F2 背壓（`cases/f02_backpressure.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f2-queue-full-backpressure | #81 | 對 COM0 連發 submit `sleep 2`（不等結果）直到收到 `SESSION_QUEUE_FULL`（上限＋5 次內必現）；收到後 cancel／等佇列排空、`echo ok` 恢復正常 |
| f2-history-bounded-rss | #81 | 記 daemon RSS（`systemctl show -p MainPID` → `/proc/<pid>/status` VmRSS）→ 200 次 `submit_and_wait "echo x"` → RSS 增量 <30MB（淘汰生效；門檻寬鬆防 flaky）＋`cmd status` 對最舊 cmd_id 已淘汰或仍可查（記 evidence，不 FAIL） |
| f2-recovery-flushes-queue | #128 | 塞 3 個 pending sleep → `session recover --selector COM0` → recover 後 `submit_and_wait "echo ok"` 立即成功且無 `SESSION_QUEUE_FULL` 連鎖（灌後即測） |

### Task 14: F4 狀態語義（`cases/f04_session_semantics.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f4-activity-classification | #34 | 兩板安靜時 `session activity --selector` 回報含分類欄位且非空；送 `echo` 後分類反映活動（欄位名先 --help 確認；quiet-suspicious 亦屬合法「可區分」值，記 evidence） |
| f4-background-result-tail-consistent | #28 | background submit `for i in $(seq 1 50); do echo L$i; done` → `cmd result-tail` 迴圈收齊 → 拼接含 L1..L50 各恰一次（不漏不重） |
| f4-interactive-line-cmd-defined | #26 | `interactive-open` COM0 → `cmd submit` line 命令 → 必須「明確拒絕（error_code）或排隊後完成」二擇一；不得 accepted 後 PROMPT_TIMEOUT；收尾 `interactive-close` |

### Task 15: F7 檔案傳輸（`cases/f07_file_transfer.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f7-binary-roundtrip-md5 | #32 #21 | 產 64KB 隨機 bytes→gzip（含 null byte）→ `file push` 到 `/tmp/swreg.bin` → `file pull` 回 → md5 一致；板缺 `base64`/`md5sum` → SKIP(`target_tool_missing`) |
| f7-larger-file-not-truncated | #21 | 1MB 隨機檔 push→pull → 大小與 md5 一致（不靜默截斷）；逾時放寬（timeout 120s） |

### Task 16: F8 daemon 單一性（`cases/f08_daemon_singleton.py`）
| case id | issues | 步驟／oracle |
|---|---|---|
| f8-foreign-holder-reported | #101 #53 | tmux 開 minicom console → `daemon status` 的 `foreign_holders` 出現對應 tty 持有者；關 console 後消失 |
| f8-second-daemon-detected | #101 | 以 `guards.ThrowawayDaemon`（**不綁任何裝置**、空 by-id sandbox）起第二 daemon → prod `daemon status` `multi_open` 報出它＋`doctor` `single_daemon` 檢查反映；收尾 kill |

### Task 17: F9 開機/U-Boot（`cases/f09_boot_uboot.py`，全 destructive）
| case id | issues | 步驟／oracle |
|---|---|---|
| f9-reboot-autoboot-unmolested | #130 | `submit "reboot"` COM0 → 等 boot（`boot_wait_s`）→ session 回 READY（若 probe 打斷 autoboot 會卡 `=>` 永不 READY）；evidence 記 `log tail-text` 開機段 |
| f9-quiet-window-agent-passthrough | #130 | reboot 後 READY 即刻 `submit_and_wait "echo after_boot"` 成功（quiet window 只擋 system probe 不擋顯式命令） |
| f9-attach-during-boot-reprobes | #69 #14 #20 | `submit "reboot"` → 立即 `session clear`＋`session attach`（撞開機窗、預期先失敗）→ **不介入**，輪詢 session list：須自動 reprobe 終回 READY（≤boot_wait_s）；卡 DETACHED 不動即 FAIL(`stuck_detached`) |
| f9-uboot-readonly-and-console-kept | #44 #130 | tmux 開 console → 經 `UBootConsole`：reboot、`interrupt_autoboot()`（3s 窗）→ `readonly_cmd("printenv")` 有輸出→期間 console 仍掛著（console-list 不掉）→ `leave("boot")` → `ensure_ready`；COM1 無 U-Boot banner → 該板 SKIP(`uboot_not_present`) |
每 case 收尾 `guards.ensure_ready`；板未回 READY → FAIL＋`recover_boards`。COM0 先行、COM1 首跑時確認 banner。

### Task 18: F10 登入帳密（`cases/f10_login_creds.py`，destructive）
| case id | issues | 步驟／oracle |
|---|---|---|
| f10-unresolved-creds-terminal | #140 | prod `device release` COM1（bcm，#140 原始情境）→ `ThrowawayDaemon`（by-id sandbox 只放該線；profile 指向**不存在的 env_file**）→ throwaway `session attach` → 終態 error `CREDENTIALS_UNRESOLVED`（session list/attach 回應）；throwaway WAL TX 不得出現連續空 login 敲擊（讀 sandbox wal 檔計數）；不自動 reprobe 重試 |
| f10-creds-fixed-then-ready | #140 | 同 sandbox 補上正確 env_file → `session recover`／re-attach → READY（bcm 登入路徑恢復） |
收尾：kill throwaway → prod `device attach`（reclaim）→ `ensure_ready(COM1)`。bcm 帳密自 prod profile 既有 env 來源取得（case 內讀 prod profile 宣告，不硬編碼）。

---

### Task 19: 文件（README＋docs）

**Files:** Modify `README.md`；Create `docs/regression-plugin.md`

- [ ] README 中英雙語各加「TestPilot 回歸測試／TestPilot Regression Tests」節：定位一句、安裝（testpilot venv `pip install -e regression/`）、執行（`testpilot run serialwrap_regression`、`--case <id>`）、`allow_destructive` 說明、何時跑（改動後快跑非破壞集／發版前全跑）、指向 docs。兩語內容一致。
- [ ] `docs/regression-plugin.md`：family↔issue 對照表（Task 9–18 的表格彙整）、testbed.yaml 鍵說明、U-Boot 唯讀護欄（允許/禁止清單）、**新增 case SOP**（修好 bug → 判定實機-only → 選 family 或新 family → 寫 case（模板）→ 掛 issues → 單測 → 真機跑過）。
- [ ] Commit：`docs: TestPilot 回歸測試使用說明與新增 case SOP`

### Task 20: CLAUDE.md 政策＋PR template

**Files:** Modify `CLAUDE.md`、`.github/pull_request_template.md`

- [ ] CLAUDE.md「測試政策」後新增小節「回歸 case 政策（#155）」：修復 bug issue 時必須評估——pytest/mock 可覆蓋→加 pytest；需實機才驗得到→必須在 `regression/` 加 testpilot case（掛 issue 編號）；PR 描述須記錄評估結論（新增了哪個 case，或為何免加）。
- [ ] PR template checklist 加：`- [ ] 回歸 case 評估已記錄（pytest／regression case／免加理由）`
- [ ] Commit：`docs(policy): 修 bug issue 必評估回歸 case 歸屬`

### Task 21: changelog fragment＋收尾驗證

**Files:** Create `changelog.d/155-testpilot-regression-plugin.md`

- [ ] fragment（type: added）：一句話＋family 數＋case 數。
- [ ] `python3 -m pytest -q tests/` 無新失敗；`python3 -m policy_check --repo .` 綠。
- [ ] Commit。

---

## 交付驗證（plan 外的流程 gate，見 openspec tasks 5.x/6.x）

venv 升級（#154 ops）→ editable 安裝＋`testpilot list-plugins` 煙霧 → sonnet subagent 真機全案（非破壞集＋`allow_destructive: true`，bench 空檔，會 reboot 兩板）→ openspec archive（R-22／Purpose 勿 TBD）→ push → PR（`Closes #155`）。
