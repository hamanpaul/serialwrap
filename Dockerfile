FROM python:3.11-slim

# 注意：容器通常不含 systemd，serialwrap setup 會自動退回 on-demand 監管模式。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        jq \
        minicom \
        socat \
        openssh-server \
        openssh-client \
        autossh \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/serialwrap

COPY . /opt/serialwrap

# pyproject.toml 帶入 PyYAML 依賴；console_scripts 自動放到 PATH。
RUN pip install --no-cache-dir .

# ── tools/docker/remote_tunnel_test.sh（docker 三拓樸驗收）用 sshd／使用者／金鑰預燒 ──
# `tester`：容器間 passwordless ssh 帳號（uart -> agent/relay，皆用同一把 baked key，
#   因每個角色容器都是同一 image）。`otheruser`：斷言⑥「relay 上第二個本機使用者」
#   trust-boundary 案例用，無 ssh key、僅供 `docker exec -u otheruser` 本機驗證。
RUN useradd -m -s /bin/bash tester \
    && useradd -m -s /bin/bash otheruser \
    && passwd -d tester \
    && passwd -d otheruser \
    && mkdir -p /home/tester/.ssh /home/tester/.sw-relay \
    && ssh-keygen -q -t ed25519 -N "" -f /home/tester/.ssh/id_ed25519 -C serialwrap-remote-tunnel-test \
    && cp /home/tester/.ssh/id_ed25519.pub /home/tester/.ssh/authorized_keys \
    && chmod 700 /home/tester/.ssh /home/tester/.sw-relay \
    && chmod 600 /home/tester/.ssh/authorized_keys /home/tester/.ssh/id_ed25519 \
    && chmod 644 /home/tester/.ssh/id_ed25519.pub \
    && chown -R tester:tester /home/tester \
    && ssh-keygen -A

# ssh client：測試容器間互連免 host-key 確認（僅此測試映像；passwordless key 已限定用途，
# 不對外流通）。**僅**寫入 `tester`（唯一會執行 `serialwrap remote`／因而 spawn ssh 的帳號，
# 見 tools/docker/remote_tunnel_test.sh 全數 `docker exec -u tester ... serialwrap remote`）
# 的 per-user `~/.ssh/config`，不動全域 `/etc/ssh/ssh_config`——避免弱化映像內其他使用者／
# 行程（如 otheruser、root）的 host-key 驗證基準。OpenSSH 依序讀 command-line → per-user
# config → 系統 config，同鍵取第一個設定值，故 per-user config 已足夠權威、無需碰系統檔。
RUN printf 'Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n    LogLevel ERROR\n' \
      > /home/tester/.ssh/config \
    && chown tester:tester /home/tester/.ssh/config \
    && chmod 600 /home/tester/.ssh/config

# sshd 預設：GatewayPorts no（loopback-only remote bind，R2 finding 2 的安全基準）。
RUN sed -i \
      -e 's/^#\?GatewayPorts.*/GatewayPorts no/' \
      -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
      -e 's/^#\?PermitRootLogin.*/PermitRootLogin no/' \
      -e 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' \
      -e 's/^#\?AllowTcpForwarding.*/AllowTcpForwarding yes/' \
      /etc/ssh/sshd_config \
    && grep -q '^GatewayPorts' /etc/ssh/sshd_config || echo 'GatewayPorts no' >> /etc/ssh/sshd_config \
    && grep -q '^PubkeyAuthentication' /etc/ssh/sshd_config || echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config \
    && grep -q '^AllowTcpForwarding' /etc/ssh/sshd_config || echo 'AllowTcpForwarding yes' >> /etc/ssh/sshd_config \
    && grep -q '^UsePAM' /etc/ssh/sshd_config && sed -i 's/^UsePAM.*/UsePAM no/' /etc/ssh/sshd_config || echo 'UsePAM no' >> /etc/ssh/sshd_config \
    # 另備 GatewayPorts yes 版本（唯一差異），供 R2 finding 2 fail-closed 斷言（#141 §11.2 斷言⑧）。
    && sed 's/^GatewayPorts no/GatewayPorts yes/' /etc/ssh/sshd_config > /etc/ssh/sshd_config.gwyes

CMD ["bash"]
