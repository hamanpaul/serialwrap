# Design: serialwrap-reliability-plugin

> 權威設計＝`docs/superpowers/specs/2026-07-20-serialwrap-reliability-testpilot-plugin-design.md`（brainstorm 逐段核可）。本檔為 OpenSpec 摘要，衝突時以權威設計為準。

## Context

- realhw（#122）：29 case、獨立 stdlib-only、測部署後系統、不進 wheel、`python3 -m realhw`。
- testpilot-core plugin 契約（已實地考證）：entry_points `testpilot.plugins`＋`PluginBase(api_version="1.1")`；4 個 abstract（`name`/`discover_cases`/`execute_step`/`evaluate`）；diagnostic_status 6 值與 category→status 映射**在 core**（`_classify_diagnostic_status`），plugin 只在 `case["_last_failure"]` 標 category tag；timeout 是軟數字（core 不 kill）；retry 可由 `execution_policy` 關掉；`create_reporter`/`report_formats` 可覆寫；default run_loop 的 wifi band 攤平對無 band case 無害（已驗證）。
- plugin 安裝慣例：editable install（wifi_llapi 與 testpilot 本身皆 `_editable_impl_*.pth`），永不進 release wheel。
- `serialwrap remote`（PR #143）：docker 三拓樸 harness 為全容器封閉世界（容器 daemon＋假 UART），缺部署 daemon＋真板覆蓋。
- 雙 daemon 雙世界：Windows 原生 serialwrapd 與 WSL 受測 daemon 共享 USB 樹；usbipd detach 後 Windows 端會抓走 COM port（0718 報告）；救援＝Windows 端 `device release`（可從 WSL 經 `/mnt/c/...` 呼叫）。

## Goals / Non-Goals

**Goals:**
- realhw 一個引擎、兩個前端（standalone CLI 與 testpilot plugin），逐案 verdict 一致。
- remote 能力的部署後真機覆蓋（rm-live）＋工具鏈迴歸（rm-topo 包裝 harness）。
- 四類分診（FailEnv/FailConfig/FailTest/Inconclusive）落在正確的桶——受測物反轉原則。
- hp-cycle 全自動救援（Windows 端 release），attended 只剩 fallback。
- release wheel 與部署端零感知。

**Non-Goals:**
- lr-mixed remote worker（隧道 48h 耐久）、`-L` 連真遠端 daemon、PassAfterRemediation、wifi_llapi 側共用 benchlock——皆 v2。
- 不改 testpilot-core、不改 wifi_llapi、不改 sw_core 生產碼。

## Decisions

1. **Thin Adapter**：plugin `execute_step` black-box 呼叫 realhw `case.run(ctx)`，不用 testpilot transport 表達 tmux/usbipd/systemd 動作；testpilot 只負責選擇、分診、trace、報表。
2. **雙發行單位**：主 pyproject（wheel）不動；`reliability/pyproject.toml`＝dev-only dist（`testpilot-core>=0.3.4,<1.0`＋entry point）。editable 下 `__file__` 在 repo 內 → `sys.path` 插 repo root → `import realhw` 零打包技巧。
3. **plugin 薄殼**：邏輯在不 import testpilot 的 `core.py`（case dict 映射、`_last_failure` 抄寫、cfg 合成）→ serialwrap CI 可測；`plugin.py` 只做 PluginBase glue。
4. **config 單一 loader 雙來源**：`realhw.load_cfg()` 吃 config.json（standalone）或 testbed.yaml 合成 dict（plugin）；等價性單測保證；Windows 端 serialwrap.exe 路徑進 testbed。
5. **分類契約**：`CaseResult.category`（environment|session|configuration|test）＋`reason_code`（自由字串）；case 內斷言失敗預設 `test`（板卡健康由 preflight＋case 間恢復保證——單一裁決線）；執行期 SKIP＝`environment`；未捕捉例外＝空 category→Inconclusive。選擇期排除（destructive 未 opt-in）在 `prepare_run` 過濾、不進報表。
6. **preflight 兩級判決**：suite-refuse（六項＋benchlock）與 family-gate（capabilities dict→requires 未滿足的 case 執行期 SKIP）。benchlock 歸 realhw preflight（standalone 同樣受保護、plugin 天然繼承）。
7. **longrun checkpoint-case**：default run_loop＋`execution_policy{sequential, max_concurrency:1}`＋`retry.max_attempts:1`＋always-pass step criteria；判決集中收尾 evaluate 讀 longrun-analysis；進度監控走 realhw 自寫 `snapshots.ndjson`。
8. **hp 救援鏈為純決策函式**：注入探測結果→回傳 action 序列；subprocess 執行薄層分離，可完整單測。
9. **rm-topo 包裝而非移植**：`remote_tunnel_test.sh` 加 `$1` 逐拓樸分派（微改），realhw case shell out、exit code＋log 尾段→verdict＋evidence；image 建置延遲到第一個 rm-topo case。

## Risks / Trade-offs

- **testpilot API 演進**：`api_version` pin＋薄殼縮小接觸面；band 攤平若未來變不相容→fallback `create_runner()`。
- **雙前端一致性受真機偶發影響**：驗收準則含歸因程序（adapter bug vs 偶發），歸因不出視為 adapter 缺陷。
- **rm-live 與 harness 容器互擾**：不同容器名前綴、各自 teardown、rm-live 不碰 harness 的 `SERIALWRAP_RUN_DIR`。
- **部署落後 repo**：capabilities family-gate＋`deployed_daemon_stale` 歸因＋報表身分烙 deployed 版本；首次 run 前 redeploy 為營運前置。
