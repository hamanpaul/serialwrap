# fix/wrapped-echo-false-positive 雲端 preflight 結果（34c3a84）

- 執行日期：2026-10-28（雲端容器，排程自動執行）
- serialwrap：`fix/wrapped-echo-false-positive` @ `34c3a84`
- paulsha-conventions：`572cd48abaea38e176eeed2464abf85e5925c6f9`
- 命令：`OPENSPEC_TELEMETRY=0 DO_NOT_TRACK=1 bash <conventions>/skills/preflight-ci/scripts/preflight.sh --pr-title "fix(command): 折行回顯剝除不再以去空白比對，避免誤刪剛好等於命令去空白的輸出" --pr-body-file /tmp/pr-body.md --pr-labels "" --base main`
- 環境：`nproc` = 4；loadavg 最後一輪 preflight 前 `0.13 0.18 0.09`、後 `0.20 0.18 0.10`；以 root 身分執行
- 工具：openspec CLI 1.15.0（`npm i -g @fission-ai/openspec` 當下最新版）、universal-ctags、pytest 9.2.0、PyYAML、jsonschema、pyserial 3.5

## 結論

**PREFLIGHT FAIL**。但**沒有一項失敗是本分支造成的**：

| step | 結果 | 秒數 | 說明 |
|------|------|------|------|
| engine | PASS | 0.01 | |
| policy | PASS | 2.80 | |
| openspec | FAIL | 0.32 | main 上同樣失敗（同一個 ERROR、同樣 12 passed／15 failed）；本分支沒改 `openspec/` |
| tests | FAIL | 127.52 | 2 個失敗，皆因容器以 root 執行、`chmod` 擋不住 root；改用一般使用者跑皆 PASS |

## 補裝工具與重跑經過

共跑 5 次 preflight，每次都整個重跑：

1. 第 1 次：policy FAIL，缺 universal-ctags（doc-drift symbol 分析需要）→ `apt-get install -y universal-ctags`。
2. 第 2 次：policy PASS；tests FAIL 0.02s，因為 `pip` 裝到的 interpreter 不是 `python3` → 改用 `python3 -m pip install` 安裝 pytest 等。
3. 第 3 次：tests FAIL 2.49s，收集期錯誤：`tests/test_flash_pump.py:50` `SyntaxError: f-string expression part cannot include a backslash`。容器的 `python3` 是 3.11，該檔使用 3.12 才支援的 f-string 語法（PEP 701）。CI（`.github/workflows/tests.yml`）固定 `python-version: "3.12"`，因此在 repo 外放一個 `python3 → /usr/bin/python3.12` 的 PATH shim，並替 3.12 安裝依賴。
   - 附帶觀察：`pyproject.toml` 宣告 `requires-python = ">=3.10"`，但測試在 3.10／3.11 收集即失敗；這不在本 PR 範圍內，僅記錄。
4. 第 4 次：tests FAIL，3 個失敗，其中 `test_doctor.py::TestWindowsDoctor::test_pyserial_ok_when_importable` 是缺 pyserial（CI 會顯式安裝）→ 安裝 pyserial。
5. 第 5 次（最終）：結果如上表。

## pytest 結果（python3.12，最終一輪）

`python3 -m pytest -q tests/ -rf`：**1984 passed、17 skipped、2 failed**（另有 92 subtests passed），127.72s。

失敗的 test：

1. `tests/test_bench_tools_cli.py::test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches`
   - 錯誤：`AssertionError: assert 0 != 0`（`tools/bench-issue.sh eit-06` 回傳 0）。
   - 原因：測試以 `benches_dir.chmod(0o555)` 製造本地寫入失敗，root 不受目錄權限限制，寫入成功，腳本照常完成。
2. `tests/test_session_capture.py::TestLogStartErrorPaths::test_open_permission_denied`
   - 錯誤：`AssertionError: True is not false`（`log_start` 回傳 `ok: True`）。
   - 原因：同上，測試以 `chmod(0o444)` 製造無寫入權限，root 照樣寫得進去。

單獨重跑（root）：

| test | 第 1 次 | 第 2 次 |
|------|---------|---------|
| `test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches` | FAIL | FAIL |
| `test_open_permission_denied` | FAIL | FAIL |

失敗穩定重現、不是 flaky。另把 repo 複製一份，交給非 root 使用者以 python3.12 跑這 2 個 test：**2 passed**。可確定是 root 環境造成，與本分支無關（本分支只改 `sw_core/session_manager.py` 的回顯剝除、`tests/test_wrapped_echo_strip.py`、spec 文件與 changelog fragment）。

## openspec 失敗明細

`openspec validate --all --strict`：12 passed、15 failed（27 項）。唯一的 ERROR：

> `change/minicom-console-lifecycle-robustness` 的 `session-interactive/spec.md`：MODIFIED "dead orphan console SHALL be detached via liveness" 漏掉現行 spec 的 scenario "dead console peer is detached"。

其餘 14 項是 `--strict` 把 WARNING 當失敗：Purpose 段仍是佔位文字、requirement 文字超過 500 字等。這可能是 openspec CLI 1.15.0 新增的檢查，repo 與 CI 都沒有固定 openspec 版本。`origin/main` 上結果完全相同，本分支的 diff 也不含 `openspec/`。

## preflight 輸出（最終一輪全文，共 6 行，未滿 40 行）

```text
context: base=main head=fix/wrapped-echo-false-positive labels=0 visibility=unknown
engine: PASS 0.01s (skill:hamanpaul/paulsha-conventions@572cd48abaea38e176eeed2464abf85e5925c6f9)
policy: PASS 2.80s (exit=0)
openspec: FAIL 0.32s (exit=1)
tests: FAIL 127.52s (exit=1)
PREFLIGHT FAIL
```
