# OD-33 R2(b)：0.3.2 正式版 → 0.4.0 候選的升級驗證容器（Linux systemd）。
# 基底與 systemd.Dockerfile 相同（OD-16 (5) 的 ubuntu:24.04 digest，glibc 2.39／Python 3.12.3，與 host 建置的 dev bundle 相容）。
# 只裝執行升級情境所需：systemd（user manager 經 linger 啟動）、pipx（0.3.2 正式安裝路徑）、jq／minicom 與 dialout
# 群組（doctor 的對應檢查在一般安裝機上應為綠燈，容器內不得以「環境缺件」帶過）。不放 repo 內容：
# 0.3.2 wheel、0.4.0 bundle 與容器內 driver 一律以唯讀 bind mount 提供。
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash ca-certificates dbus-user-session jq login minicom pipx python3 python3-venv systemd systemd-sysv util-linux \
    && if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi \
    && useradd --create-home --uid 1000 --shell /bin/bash --groups dialout swtest \
    && h="$(getent passwd swtest | cut -d: -f6)" \
    && install -d -o swtest -g swtest "$h/.config" "$h/.config/systemd" "$h/.config/systemd/user" \
    && install -d /var/lib/systemd/linger \
    && touch /var/lib/systemd/linger/swtest \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

STOPSIGNAL SIGRTMIN+3
VOLUME ["/sys/fs/cgroup"]
CMD ["/sbin/init"]
