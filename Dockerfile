FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/serialwrap \
    PATH=/opt/serialwrap:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        jq \
        minicom \
        socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/serialwrap

COPY . /opt/serialwrap

RUN pip install --no-cache-dir pyyaml pyserial \
    && chmod +x /opt/serialwrap/serialwrap \
    && chmod +x /opt/serialwrap/serialwrapd.py

CMD ["bash"]
