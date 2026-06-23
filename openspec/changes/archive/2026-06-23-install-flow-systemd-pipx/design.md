## Context

serialwrap 是「常駐 daemon（獨佔 `/dev/ttyUSB*`、唯一 reader、維持狀態）＋輕量 CLI client（Unix socket）」這一類工具，需 `dialout` 權限。現況安裝缺口：無打包、裸借系統 PyYAML、daemon 不常駐（靠 `minicom_router` 順手帶起）、狀態在 `/tmp`（會被 reaper 掃）、`minicom` 靠 PATH-shadow。本變更面向「對外發佈、不受控環境可裝」。完整逐節設計：`docs/superpowers/specs/2026-06-22-install-flow-systemd-pipx-design.md`。

## Goals / Non-Goals

**Goals:**
- `pipx install "git+https://github.com/hamanpaul/serialwrap@<tag/SHA>"` 即可安裝，依賴隔離於 pipx venv。
- daemon 以 systemd 常駐為主、可開機自啟/crash 拉回；無 systemd 平台（含未啟用的 WSL2）退回 on-demand 降級備援。
- 安裝/設定全程 user-level 可完成；需 root 的動作只印指令、不靜默 sudo。
- 跨重裝可在 on-demand ↔ systemd 之間乾淨轉換，且任何時刻只有一個 daemon。
- 既有 `SERIALWRAP_*` env 覆寫與 throwaway-daemon/CI 跑法不受影響。

**Non-Goals:**
- 原生 macOS/Windows 支援（serial 路徑/權限模型不同）。
- PyPI 公開發佈。
- systemd socket-activation（`.socket` 單元）。

## Decisions

- **分發＝pipx + git+SHA**（而非 PyPI / GitHub Release wheel / 維持 git clone+install.sh）。理由：沿用 org 既有 pin-SHA 慣例（policy engine 同款）、免 PyPI 帳號與公開曝光、pipx 自動隔離 venv 解掉裸借 PyYAML。Alternatives：PyPI（最正規但要帳號/發佈/曝光）、Release wheel（可離線但多一套產物流程）、強化 install.sh（仍需對方 git clone、最不正規）。
- **生命週期＝hybrid（systemd 為主 + on-demand 降級）**（而非純 on-demand 或純 systemd）。理由：使用者要 systemd 常駐，但外部/ WSL 未必有 systemd，需可攜 fallback。Alternatives：純 on-demand（最可攜但無常駐）、純 systemd（最正規但 WSL/無 systemd 裝不起來）。
- **單一事實來源 `config.yaml: supervision_mode` gate 掉所有 auto-spawn**。理由：systemd 與 on-demand 同時活著會互搶 socket/裝置（本專案已見 minicom_router 重拉 daemon 的 churn）。Alternative：只靠 `SingletonLock`——不足，它擋同 socket 但擋不了兩 process 同開同一 `/dev/ttyUSB*`（two-reader）。
- **setup 為冪等 reconciler、轉換「先停舊再起新」**。理由：避免交接瞬間 two-reader；先關舊 daemon 釋放 tty FD 再起新機制。
- **資產 relocate 進套件 + `importlib.resources`**（setuptools）。理由：console_scripts venv 下唯一可靠取得 data 的方式；首跑 setup 物化到使用者可寫位置。Alternative：hatchling force-include 不搬檔——使用者選擇維持 setuptools+relocate。
- **路徑改 XDG（脫離 /tmp），env 覆寫優先序最高**。理由：`/tmp` 非持久且會被 reaper 掃；env 覆寫保住既有自動化。
- **systemd 預設 user-scope（`--system` opt-in）**。理由：契合 pipx 的 user-level binary、最少 root；serial 一 tty 一 reader，per-user daemon 反而正確。

## Risks / Trade-offs

- [systemd↔on-demand 交接 two-reader] → 嚴格「先停舊釋放 FD 再起新」+ `SingletonLock` + 轉換後 health/doctor 驗證單一 daemon。
- [轉換/重裝切斷進行中燒錄或傳輸] → 轉換前偵測 `FLASHING`/前景忙碌即中止報錯，`--force` 才強制。
- [system 模式 socket 權限/可見性] → 固定 `/run/serialwrap/serialwrapd.sock`、mode 660 + 群組，`config.yaml` 記錄有效 socket 供各使用者 CLI 連。
- [WSL 無 systemd] → setup 偵測並引導 `/etc/wsl.conf [boot] systemd=true` + `wsl --shutdown`，重啟前先 on-demand。
- [systemd 沙箱誤擋 /dev] → unit 明確不設 `PrivateDevices`/`DeviceAllow`。
- [legacy `~/.paul_tools` 與 pipx binary 在 PATH 衝突] → setup 偵測並引導退役 + 遷移 `state.json`。
- [setup/doctor 難以無 root/無 systemd 測試] → 副作用走可注入 effects 介面，單元測試以 mock 驗證。

## Migration Plan

1. 加 `pyproject.toml` + relocate 資產 + `sw_core/daemon.py`（root `serialwrapd.py` 轉薄 shim），確保既有測試與 `--socket` 呼叫不破。
2. `constants.py` 改 XDG 預設、保留 env 覆寫；首跑 setup 遷移 legacy `/tmp/serialwrap/state.json`。
3. 實作 `setup`/`doctor`/`service` 子命令與 systemd unit 範本；接上 auto-spawn gate。
4. `install.sh` 轉 `pipx install + serialwrap setup`；Dockerfile 對齊；README 安裝段。
5. Rollback：env 覆寫與 root `serialwrapd.py` shim 保留，可隨時退回 on-demand / 舊路徑；未切 tag 前不影響既有使用者。

## Open Questions

- 無（六段設計已與使用者逐段確認定稿）。
