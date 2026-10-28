---
type: change
scope: rust-cli
---

Stage 1 systemd docker 實跑基底 pin 變更（OD-16 (5)，解除 BLOCKED-PREREQ-S1X-04）：`tools/docker/systemd.Dockerfile` 的基底由 `debian:12-slim`（glibc 2.36）改為 `ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac`（glibc 2.39，與 host 建置一致；Python 3.12.3）。digest 取自 `docker buildx imagetools inspect ubuntu:24.04` 的 index digest。apt 來源改為 Docker 官方 `ubuntu noble` 套件庫，`docker-ce-cli` 釘為 `5:29.8.2-1~ubuntu.24.04~noble`；新增 `python3-yaml`（bundle 的 py 執行環境以 `--system-site-packages` 建立，daemon 以 `-I` 啟動，須由系統 site 提供 PyYAML，與 host 的 `python3-yaml` 同源）；image 內預設 UID 1000 的 `ubuntu` 使用者先移除，以容納測試使用者 `swtest`（UID 1000）。原因：`test_update_systemd_exit1_not_relaunched_l15` 於 debian 基底下因 host 建置的 Rust 二進位需 `GLIBC_2.39` 而失敗（`GLIBC_2.39 not found`）。本 pin 同時影響 `scripts/docker/run-l15.sh`（其 build 步驟亦建置 systemd image）與 `run-systemd.sh`；`tools/docker/serialwrap.Dockerfile` 未變更。

同輪於 `scripts/docker/run_systemd_container.py` 的 `_run_update_exit1` 修正 harness 缺陷（非測試斷言變更；未放寬任何判定）：(1) 初次 `setup --user` 後以 `service restart` 啟動受監管 daemon 再等待 READY，與 `systemd_container.initialize_supervised` 相同（跨模式 setup 只註冊 unit）；(2) bridge 建立的 PTY slave 交給 swtest（`serve()` 既有做法）；(3) `_read_daemon_status` 的 `REPO` 改用 `systemd_container.REPO`（原為未定義名稱）；(4) `_holder_pids` 改為 root 與 daemon 使用者兩輪 `/proc` fd 掃描取聯集，仍無法讀取的現存行程拋出 RuntimeError（fail-closed）。原掃描以 root 執行，容器 root 無 CAP_SYS_PTRACE，讀不到 swtest 行程 fd 表，`OSError` 被吞為「無持有者」，會使 holder 判定恆真。

實跑證據：`tests/contract_blackbox/test_cli_mgmt_update.py::test_update_systemd_exit1_not_relaunched_l15` 1 passed（report verdict PASS、`evidence_layer: "L1.5-docker"`、producer `run-systemd.sh`、old daemon exit 1、NRestarts 0、holder marks 成對、current release 0.4.1）；`scripts/docker/run-systemd.sh --scenario hold` 亦 PASS。
