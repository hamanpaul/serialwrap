# Proposal: UART RX Event Trigger Engine（Issue #37）

## 問題陳述

serialwrap broker 目前只能被動傳輸 UART 串流，無法對收到的特定字串自動觸發動作。agent 必須輪詢 WAL 或自行剖析輸出，高延遲且易漏事件。

## 目標

實作 v1 UART RX → pattern → handler trigger engine：一個 udev/crontab 式宣告規則引擎，緊鄰 bridge，以 fire-and-forget subprocess 執行 handler，**從不阻塞 UART IO**。

## 範圍

- 規則格式：JSON/YAML 存於 `~/.serialwrap/events.d/<owner.name>.json`
- 模式比對：contains / regex / starts_with / ends_with
- 閘道（gating）：selector（COM filter）、scope（自發/命令輸出/全部）、max_fires、cooldown_ms、profile filter
- Dispatcher：subprocess 生成、timeout + pgid kill、per-rule concurrency=1、stdin JSON payload、env subset
- COM enable/disable：預設 disabled，auto_enable_com_on_load opt-in
- EventLogger：NDJSON append、rotation、tail（含 filter）
- RPC/CLI/MCP：10 event.* 路由完整覆蓋
- 安全契約：descriptions 必須在 enable/disable 前呼叫 status

## 不在範圍

- 跨 session 事件聚合
- 複雜 CEP（複合事件處理）
- UI dashboard
