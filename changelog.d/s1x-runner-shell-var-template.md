---
type: fix
scope: func-test
---

修正 func-test runner 的未解析樣板檢查（`TestRunner._require_resolved`）把合法的 shell 參數展開誤判為樣板的缺陷：原本以 `\{([a-zA-Z0-9_.]+)\}` 掃描展開後的字串，`${HOME}` 這類 DUT 端 shell 形式會被判成未解析樣板並拋出 StepError，例如 `cmd submit --cmd "echo ${HOME}"` 在送出前即被拒絕。

現行判準（`func-test/runner.py::_UNRESOLVED_TEMPLATE_RE`）：裸 `{var}`／`{var.field}`（前面不得緊接 `$`）與誤加 `$` 前綴的點號形式 `${var.field}` 仍視為未解析樣板並 fail-closed；`${NAME}`（不含點號）與 `$NAME` 為字面字串，原樣送出。`${var.field}` 含點號、不是合法 shell 識別字，因此仍予拒絕，避免 `${after_cmd.cmd_id}` 這類樣板因誤加 `$` 而漏檢。

已知殘留（未於本次處理）：(1) 不含點號的 `${NAME}` 若實為未就緒的 harness 樣板，無法與 DUT 端 shell 參數從字串區分，會被視為字面字串放行；(2) `_expand` 仍會把 `${NAME}` 中的 `{NAME}` 替換為同名 save_as 變數的值，與 save_as 名稱同名的 shell 變數會被展開錯誤，此行為未變更。

測試：`tests/test_functest_harness.py` 新增兩組案例，分別驗證 shell 形式（`${HOME}`、`$HOME`、`${A:-b}`）通過檢查，以及未解析樣板（含 `{after_cmd.cmd_id}`、`${after_cmd.cmd_id}`）仍拋出 StepError 且錯誤訊息不列出合法的 shell 形式。
