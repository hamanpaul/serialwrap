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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/serialwrap

COPY . /opt/serialwrap

# pyproject.toml 帶入 PyYAML 依賴；console_scripts 自動放到 PATH。
RUN pip install --no-cache-dir .

CMD ["bash"]
