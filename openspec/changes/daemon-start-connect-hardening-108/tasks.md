## 1. RED 測試（先寫失敗測試，確認為對的理由失敗）

- [ ] 1.1 `tests/` 新增 #1 測試：systemd 模式 `serialwrap daemon start` 重導 `service_action("start", ...)`（mock service_action / fx），斷言不呼叫 `subprocess.Popen` 且回應含 `_routed_to: "service start"`
- [ ] 1.2 新增 #1 測試：on-demand 模式已有健康 daemon（mock `health.ping` ok）時 `daemon start` 回 `already_running: true` 且不 spawn；無回應時才 spawn
- [ ] 1.3 新增 #2 測試：`_resolve_endpoint` 在 config `socket_path` 死 + `systemd-system` → 回 `SYSTEM_SOCKET`（mock `_endpoint_alive`）；config socket 活 → 原樣；兩者皆死 → 回原 `socket_path`；明確 `--socket`/`--endpoint` 不被 fallback 覆蓋
- [ ] 1.4 跑 `python3 -m pytest -q tests/ -k "daemon_start or resolve_endpoint or supervision"`，擷取 RED 輸出，確認失敗原因符合預期（函式/分支尚未實作）

## 2. Item #1 — daemon start 監管模式 gate + 冪等

- [ ] 2.1 `sw_core/cli.py`：`daemon start` subparser 補 `--with-sudo`（dest=`with_sudo`，鏡像 `daemon stop`）
- [ ] 2.2 `_run_daemon_start` 開頭：讀 `_default_runtime_config().mode()`，`mode.startswith("systemd")`（即 `not should_auto_spawn()`）→ `service_action("start", mode=mode, with_sudo=args.with_sudo)`，回應加 `_routed_to: "service start"`、`_print` 後 return（鏡像 `_run_daemon_stop`）
- [ ] 2.3 on-demand 分支：spawn 前對目標 endpoint `health.ping`（timeout 0.5s），命中健康 daemon → 回 `{ok: true, already_running: true, socket: ...}` no-op；否則沿用既有 spawn 流程
- [ ] 2.4 跑 1.1/1.2 對應測試轉 GREEN

## 3. Item #2 — _resolve_endpoint dangling-socket fallback

- [ ] 3.1 `sw_core/cli.py`：新增 `_endpoint_alive(ep) -> bool` helper（unix path 0.2s connect probe；非 unix endpoint 一律 True）
- [ ] 3.2 `_resolve_endpoint`：當選用來源為 config-derived `cfg_sock`、為 unix path 且 `not _endpoint_alive` 時，依 `mode()` 推 canonical（`systemd-system→SYSTEM_SOCKET`，其餘→`SOCKET_PATH`）；canonical 可連且 ≠ cfg_sock → 改用之 + stderr 一行提示；否則回原 `cfg_sock`。明確 `--endpoint`/非預設 `--socket` 維持最高優先、不進此分支；不改寫 config.yaml
- [ ] 3.3 跑 1.3 對應測試轉 GREEN

## 4. 文件與對齊

- [ ] 4.1 `README.md` / `docs/**` 對齊 `daemon start` 新行為（systemd `_routed_to`、on-demand `already_running`、dangling fallback 提示）（R-16/R-18）
- [ ] 4.2 `CHANGELOG.md` `[Unreleased]` 補本變更；含 #4 結案說明（不加 `/tmp/sw-coexist-*` / dead-pid lock production GC 之理由）

## 5. 驗證 / Gate

- [ ] 5.1 `python3 -m pytest -q tests/` 全跑，確認無新增失敗（pre-existing flaky 除外）
- [ ] 5.2 `python3 -m policy_check --repo .` 通過
- [ ] 5.3 擷取 GREEN 輸出佐證；確認 openspec `applyRequires` 完成
