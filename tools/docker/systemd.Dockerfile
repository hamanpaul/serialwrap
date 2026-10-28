# OD-16 (5)：基底由 debian:12-slim（glibc 2.36）改為 ubuntu:24.04（glibc 2.39，與 host 建置一致；Python 3.12.3）。
# digest 取自 `docker buildx imagetools inspect ubuntu:24.04` 的 index digest。pin 變更見 changelog.d/rust-s1x-systemd-base-ubuntu2404.md。
# python3-yaml：dev bundle 的 py 執行環境以 --system-site-packages 建立（mkbundle.sh），daemon 以 `-I` 啟動、只能讀系統 site 的 PyYAML（與 host 的 python3-yaml 同源）。
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG DOCKER_CE_CLI_VERSION=5:29.8.2-1~ubuntu.24.04~noble

ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$(dpkg --print-architecture)" \
       > /etc/apt/sources.list.d/docker.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       bash dbus-user-session "docker-ce-cli=${DOCKER_CE_CLI_VERSION}" login python3 python3-pip python3-venv python3-yaml sudo systemd systemd-sysv util-linux \
    && if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi \
    && useradd --create-home --uid 1000 --shell /bin/bash swtest \
    && install -d -o swtest -g swtest /home/swtest/.config/systemd/user \
    && install -d /opt/serialwrap \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/serialwrap
COPY . /opt/serialwrap
RUN python3 -m venv /opt/serialwrap/.venv \
    && /opt/serialwrap/.venv/bin/pip install --no-cache-dir . \
    && /opt/serialwrap/.venv/bin/pip install --no-cache-dir -e 'tools/uart_stdio_bridge[dev]'

STOPSIGNAL SIGRTMIN+3
VOLUME ["/sys/fs/cgroup"]
CMD ["/sbin/init"]
