## 1. 打包基礎（packaging-distribution）

- [ ] 1.1 把 root `serialwrapd.py` 的 `main()`/`BLOCKING_RPC_METHODS` 搬到 `sw_core/daemon.py`，root 檔轉薄 shim（`from sw_core.daemon import main`）；既有測試與 `--socket` 呼叫不破
- [ ] 1.2 relocate 資產到 `sw_core/assets/{profiles,tools,skill}`（git mv），加 `sw_core/assets.py`（`importlib.resources` 取用器）
- [ ] 1.3 新增 `pyproject.toml`（setuptools、PEP 621）：`serialwrap=sw_core.cli:main`、`serialwrapd=sw_core.daemon:main`、`dependencies=["PyYAML>=6"]`、`requires-python>=3.10`、package-data 含 assets、version 與 `VERSION` 同源
- [ ] 1.4 測試：`python -c "import sw_core"`、entry points 可解析、`importlib.resources` 讀得到 assets；在乾淨 venv `pip install .` smoke

## 2. 路徑/XDG（runtime-paths）

- [ ] 2.1 重構 `sw_core/constants.py`：未設 env 時 user 範圍走 XDG（runtime/state/config/data），system 範圍走 `/run|/var/lib|/etc`；保留所有 `SERIALWRAP_*` 覆寫且優先序最高
- [ ] 2.2 `config.yaml` 讀寫（含有效 socket 路徑與 `supervision_mode`）；CLI 解析有效 socket 的順序：env > config.yaml > 模式預設
- [ ] 2.3 `/tmp/serialwrap/state.json` → 新位置的遷移函式（僅在新位置為空時搬）
- [ ] 2.4 測試（先寫）：XDG 解析、env 覆寫優先、缺 `$XDG_RUNTIME_DIR` 退 state/run（非 /tmp）、遷移保留 sessions/alias/RELEASED

## 3. 監管核心（daemon-supervision）

- [ ] 3.1 定義可注入的 effects 介面（run 指令、偵測 systemd、檔案/symlink、loginctl）——供 setup/doctor/service 共用且可 mock
- [ ] 3.2 `supervision_mode` 讀取 API；在 CLI lazy-start 與 `tools/minicom_router.sh` 加 auto-spawn gate（systemd 模式不 spawn、回明確錯誤；on-demand 維持）
- [ ] 3.3 測試（先寫）：systemd 模式 CLI/router 不 spawn 並回正確錯誤；on-demand 仍 spawn；`SingletonLock` 防雙開

## 4. systemd unit 與 service 子命令

- [ ] 4.1 user/system unit 範本（資源檔；明確不含 `PrivateDevices`/`DeviceAllow`）；產生器填入路徑/帳號
- [ ] 4.2 `serialwrap service {start|stop|restart|status}` 依模式包 `systemctl [--user]`；systemd 模式下 `serialwrap daemon stop` 重導 `service stop`
- [ ] 4.3 測試（先寫）：unit 內容無阻擋 /dev 指令；service 子命令對 mock systemctl 下正確參數

## 5. setup / doctor（install-setup）

- [ ] 5.1 `serialwrap setup` 物化資產（profiles 不覆蓋/`--force`；skill 物化+symlink；minicom wrapper 落 `~/.local/bin`，wrapper 內 `command -v minicom`）
- [ ] 5.2 setup reconciler：模式決策（auto-detect/`--user`/`--system`/`--on-demand`）＋轉換「先停舊釋放 FD →（必要時）遷移 state → 起新 → 驗證單一 daemon」
- [ ] 5.3 flash/忙碌護欄（FLASHING 或前景忙碌即中止，`--force` 強制）；sudo 邊界（dialout/`/etc/wsl.conf`/`--system` 預設只印指令，`--with-sudo`/互動才代跑）；WSL systemd 引導
- [ ] 5.4 `serialwrap doctor` 唯讀診斷各項並印修復指令
- [ ] 5.5 legacy `~/.paul_tools` 偵測/退役/遷移引導
- [ ] 5.6 測試（先寫）：物化冪等與不覆蓋、轉換順序與單一 daemon、flash 護欄、不靜默 sudo、doctor 判定、legacy 遷移（全走 mock effects，無需 root/systemd）

## 6. 安裝入口與文件對齊

- [ ] 6.1 `install.sh` 轉 `pipx install "<repo>" && serialwrap setup`（舊 copy 行為標 legacy）
- [ ] 6.2 `Dockerfile` 對齊（pip/pipx 安裝、移除 pyserial、容器退 on-demand）
- [ ] 6.3 `README.md` 安裝段重寫（pipx+git+SHA / setup / doctor / dialout+WSL sudo 一行 / on-demand 降級），修掉「預設 /usr/local/bin」漂移
- [ ] 6.4 CI：新增打包 smoke（build wheel → 裝 → entry points + `serialwrap doctor` 可跑）；容器 smoke 跑 func-test `fake_target` round-trip

## 7. 驗證與政策收尾

- [ ] 7.1 全套件 `python3 -m pytest -q tests/` 無新失敗；`python3 -m policy_check --repo .`（含 PR 上下文）綠
- [ ] 7.2 真機驗證：全新 pipx 裝 + setup（systemd 啟用）→ `systemctl --user status serialwrap` active、認線、`cmd submit` 通
- [ ] 7.3 真機驗證：無 systemd（on-demand）→ 啟用 WSL systemd → 重跑 setup → 乾淨轉 systemd-user、單一 daemon、state 保留
- [ ] 7.4 `CHANGELOG.md [Unreleased]`、`VERSION`（若改版）、四份 agent 檔（若有改）同步；切 git tag 供 pin
