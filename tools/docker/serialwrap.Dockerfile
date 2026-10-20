FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

ARG DOCKER_CE_CLI_VERSION=5:29.8.2-1~debian.12~bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/serialwrap:/opt/serialwrap/tools/uart_stdio_bridge/src \
    SERIALWRAP_BY_ID_DIR=/run/sw-fake/by-id \
    SERIALWRAP_BY_PATH_DIR=/run/sw-fake/by-path \
    SW_UART_KIND=pty-fake

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: bookworm\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$(dpkg --print-architecture)" \
       > /etc/apt/sources.list.d/docker.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash "docker-ce-cli=${DOCKER_CE_CLI_VERSION}" jq minicom procps socat tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/serialwrap
COPY . /opt/serialwrap
RUN python3 -m pip install --no-cache-dir . \
    && python3 -m pip install --no-cache-dir pytest==9.0.3 PyYAML==6.0.3 jsonschema==4.10.3 \
    && python3 -m pip install --no-cache-dir -e 'tools/uart_stdio_bridge[dev]' \
    && python3 -m pip install --no-cache-dir --no-deps -e regression/ \
    # 容器以主機 UID 執行（compose user: SW_CONTAINER_UID），測試會在 repo 根目錄建暫存檔與 .pytest_cache，需可寫
    && chmod -R a+rwX /opt/serialwrap

CMD ["/bin/bash"]
