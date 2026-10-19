FROM debian:12-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG DOCKER_CE_CLI_VERSION=5:29.8.2-1~debian.12~bookworm

ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: bookworm\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$(dpkg --print-architecture)" \
       > /etc/apt/sources.list.d/docker.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       bash dbus-user-session "docker-ce-cli=${DOCKER_CE_CLI_VERSION}" login python3 python3-pip python3-venv sudo systemd systemd-sysv util-linux \
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
