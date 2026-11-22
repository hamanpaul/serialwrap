# serialwrap 功能頁

架構總覽（[`../architecture.html`](../architecture.html)）畫的是元件與它們之間的關係；這個目錄的每一頁從**一個功能**的角度，畫出該功能實際走過哪些元件、依什麼順序、在什麼條件下分支。總覽與每一頁上方都有同一條功能導覽列，可以互相切換。

所有頁面描述的是原始碼 revision `12eb9a0ebead674a74bca58ffb0cd462074f0347`（main，含 0.5.0-rc.1 之後合入的 issue 282 停機修正），不是某個發行版的位元組。

## 功能清單

| # | 頁面 | 功能 | 涵蓋 |
|---|---|---|---|
| 01 | `01-command-exec` | 命令執行 | line／background／interactive 三種模式、READY 閘門、排隊與單一寫入者、取消、結果擷取與完成判定 |
| 02 | `02-session-lifecycle` | Session 生命週期 | attach、登入狀態機、ready probe、quiet、自動重新探測、recover、self-test、reboot 後恢復 |
| 03 | `03-device-mgmt` | 裝置管理 | 裝置偵測（by-id／登錄檔）、熱插拔、COM 編號確定性指派、bind／alias／pin、release／attach 交接 |
| 04 | `04-human-console` | 人類 console 共用 | PTY／Telnet 端點、raw owner 與行模式、agent 命令期間暫存與回放、多個 console、interactive lease |
| 05 | `05-file-transfer` | 檔案傳輸 | push／pull、逐段等回顯、MD5 校驗、停機時的行收尾 |
| 06 | `06-mcu-flash` | MCU 燒錄 | `/dev/ttyMCU` 端點、sync probe 認線、FLASHING 仲裁、燒錄程式斷線後自動釋放 |
| 07 | `07-event-engine` | 事件引擎 | 規則增刪查、RX 比對、外部 handler、各 COM 開關 |
| 08 | `08-logs-evidence` | 紀錄與證據 | WAL 雙軌紀錄與輪替、原始 log、per-session capture、activity |
| 09 | `09-daemon-mgmt` | daemon 管理 | 啟動序與停止序、單一實例鎖、同機多開偵測、health |
| 10 | `10-install-supervision` | 安裝與監管 | setup／更新／rollback、systemd／on-demand／Windows 工作排程監管模式、service、doctor |
| 11 | `11-remote-bench` | 遠端與測試台 | remote 隧道、connect、benches |
| 12 | `12-config-tools` | 設定與工具 | profile YAML 與測試、skill |
| 13 | `13-cross-platform` | 跨平台 | sw-pal 平台層與 Linux／Windows 後端 |

## 每一頁的檔案

`<NN>-<slug>` 為上表的頁面名稱：

- `<NN>-<slug>.facts.json`：該功能的事實檔，是語意的唯一權威（格式見下節）。
- `<NN>-<slug>.json`：原生 Archify IR，必須是 facts 的投影（不得自行新增參與者、步驟或改寫標籤）。
- `<NN>-<slug>.html`：交付頁。先由原版 Archify v3.2.0（`208acba8`）交付，再由 `tools/architecture_overlay.py` 加上導覽列與滾輪縮放（見「HTML 加工規則」）；不得手改。

## 事實檔格式（feature-facts v1）

```json
{
  "schema_version": 1,
  "document_type": "feature-facts",
  "feature": {"id": "01-command-exec", "order": 1, "title": "命令執行", "summary": "選填，一句話"},
  "repository": {"url": "https://github.com/hamanpaul/serialwrap", "revision": "12eb9a0ebead674a74bca58ffb0cd462074f0347"},
  "diagram_type": "sequence",
  "scope": {"includes": ["…"], "excludes": ["…"]},
  "participants": [
    {"id": "cli", "label": "CLI／Agent", "type": "frontend", "overview_component": "cli",
     "responsibility": "…", "basis": "observed", "evidence": [ANCHOR, …], "sublabel": "選填"}
  ],
  "steps": [
    {"id": "submit", "from": "cli", "to": "service", "label": "command.submit", "phase": "送出",
     "basis": "observed", "evidence": [ANCHOR, …],
     "condition": "選填：只在什麼條件下發生", "variant": "選填：default／dashed／emphasis／security", "note": "選填"}
  ],
  "unknowns": [{"id": "…", "question": "…", "impact": "…"}],
  "conflicts": [{"id": "…", "subject": "…", "claims": ["…"], "resolution": "…"}]
}
```

`ANCHOR` 與總覽 `facts.json` 相同：`{"path", "line", "end_line", "excerpt_sha256", "claim"}`。`excerpt_sha256` 是該檔在 `repository.revision` 上第 `line`～`end_line` 行（含行尾換行，以 `splitlines(keepends=True)` 切行後串接）的 SHA-256；`path` 不得指向 `docs/architecture/`。

規則：

- **以原始碼為準**：每個參與者與每一步都要有 evidence。`basis: inferred` 的步驟至少要有兩條獨立 evidence；查不到的寫進 `unknowns`，不要猜。
- **參與者對應總覽**：`overview_component` 填總覽 `facts.json` 的元件 id（例如 `cli`、`service`、`arbiter`、`execute`、`bridge`、`target`），讓讀者能對照；總覽沒有的細部元件（例如登入狀態機）填 `null`，但仍要有 evidence。
- **步驟依實際發生順序排列**；`phase` 是分段標籤（sequence 會畫成橫帶）；條件分支用 `condition` 寫明。每頁以一條主路徑加最重要的 2～4 條分支為原則，不逐一畫出每個 RPC。
- **跨平台差異**：Linux 與 Windows 行為不同的步驟，在 `label` 或 `note` 以「Linux｜Windows」標出，順序固定 Linux 在前。
- **Python 基線**：描述的是 Rust 實作。若某步驟與 Python 0.4.x 行為不同，寫在該步驟的 `note`。
- **圖型**：預設 `sequence`。結構性大於流程性的功能（例如 `13-cross-platform`、`12-config-tools`）可改用 `architecture` 或 `workflow`，在 `scope.includes` 第一項說明理由。
- **語言**：所有標籤、說明用繁體中文；識別字、RPC 方法名、檔名保留原文。

## IR 規則

- `meta.title`：`serialwrap｜<功能名>`；`meta.locale`：`zh-TW`；`meta.output`：`docs/architecture/features/<NN>-<slug>.html`；`meta.quality_profile`：`showcase`；`meta.repository` 與 facts 相同（加 `"provider": "github"`）。
- sequence：`participants` 的 id／label 與 facts 相同；`messages` 依序逐一對應 `facts.steps`（id、from、to、label 逐字相同），每筆必填 `y`（垂直位置）；`segments` 的 `from`／`to` 是 y 座標範圍（不是 message id），`label` 只能用 facts 出現過的 `phase`。participant 的 `sources`（最多 3 筆，`{"path","line","end_line"}`）填該參與者的 evidence 位置。官方範例：Archify 的 `archify/examples/cache-miss-request.sequence.json`。
- workflow 用 `nodes`／`edges`，architecture 用 `components`／`connections`；schema 只強制邊的 `from`／`to`，本規格另要求每條邊帶 `id` 與 `label` 並與 step 逐字對應。
- 不開瀏覽器的快速檢查：`archify.mjs validate <type> <ir.json> --quality showcase --repo-root . --json`（不到一秒）。
- workflow／architecture：節點 id 集合等於 participants、邊 id 集合等於 steps。
- 版面只調整位置與間距；不得為了排版刪掉有意義的步驟或改寫標籤。

## 驗收（每一頁都要過）

在含 `12eb9a0e` 的 serialwrap checkout 上：

```bash
python3 tools/architecture_feature_check.py docs/architecture/features/<NN>-<slug>.facts.json
ARCHIFY_UPDATE_CHECK_DISABLED=1 node <archify>/archify/bin/archify.mjs finalize sequence \
  docs/architecture/features/<NN>-<slug>.json docs/architecture/features/<NN>-<slug>.html \
  --repo-root . --quality showcase --json
ARCHIFY_UPDATE_CHECK_DISABLED=1 node <archify>/archify/bin/archify.mjs visual-check \
  docs/architecture/features/<NN>-<slug>.html --json
```

（`finalize` 的子命令與參數以 Archify v3.2.0 的 `archify.mjs --help` 為準；圖型不是 sequence 時換成對應的型別。）

- 事實檢查 0 錯誤；Archify 交付 9/9、0 error、0 warning；visual-check pass。
- 人工看過 1440×900 與 1920×1080 的截圖：第一屏是完整的圖、標籤可讀不重疊、方向清楚、順序正確。
- Archify 引擎固定用 `tt-a1i/archify` 的 `208acba8d412335185b2056fedf54b3405df43fb`（v3.2.0），不得換版、不得修改引擎。

## HTML 加工規則（擁有者 2026-11-22 核准）

Archify v3.2.0 的檢視器沒有滾輪縮放，架構圖元件也不支援超連結。擁有者核准在**不改變圖的結構與流程**的前提下，以腳本對交付的 HTML 做兩種加工，其他一律不得改：

1. **滾輪縮放**：指標在圖上時，滾輪呼叫 Archify 公開的 `Archify.view.zoomIn()`／`zoomOut()`；指標不在圖上時照常捲動頁面；`Ctrl`＋滾輪留給瀏覽器。
2. **功能導覽列**：在圖的外面加一條連到總覽與 13 個功能頁的導覽列，標出目前所在頁。

條件：

- 只能由 `tools/architecture_overlay.py` 套用，不得手改；腳本是決定性且冪等的，重複套用結果逐位元組相同。
- 加工只能放在標記區塊 `<!-- SW-OVERLAY:START … -->`／`<!-- SW-OVERLAY:END -->` 內；移除區塊後必須逐位元組還原成 Archify 原始交付的 HTML。
- 不得改動 SVG（節點、邊、標籤、幾何）、內嵌資料、來源連結與 Archify 自身的腳本；只能使用 Archify 公開的 `Archify.view` API；不得引入任何網路或外部資源。
- `toolchain.json` 記錄每頁的 Archify 原始交付雜湊、加工版本與最終雜湊；`tools/architecture_review.py` 重建時以「原版交付＋加工」驗證逐位元組一致。
- 加工後的 HTML 仍須通過原本全部的瀏覽器檢查，另外驗證滾輪確實改變縮放、導覽列每個連結都指向存在的頁面。

## 分工與交換

- 工作分支一律從 `feature/architecture-features` 分出，名稱 `feature/arch-feature-<NN>-<slug>`；只新增或修改自己那一頁的三個檔案（facts、IR、原版交付的 HTML），不要動其他頁、總覽或工具。
- 交付時 push 分支，並回報：頁面名稱、head commit、事實檢查與 Archify 交付的輸出、截圖路徑、`unknowns`／`conflicts` 摘要。
- 整合由編排者負責：合入各頁、套用加工、更新 `toolchain.json` 與測試，最後開一個 PR。
