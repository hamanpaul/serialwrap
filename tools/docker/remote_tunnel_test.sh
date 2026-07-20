#!/usr/bin/env bash
# serialwrap remote 隧道 docker 三拓樸驗收（設計 spec §11.2）。
#
# 以真 sshd + 真 `serialwrap remote`（外包系統 ssh）跑三種拓樸，全過才算通過：
#   拓樸 1 direct        uart + agent 同網段
#   拓樸 2 NAT→host      uart（NAT）+ relay 同網段，agent CLI colocate 在 relay
#   拓樸 3 NAT←client    net_a=uart+relay、net_b=agent+relay，雙 NAT，僅能經 relay
# 另跑一組額外驗收（GatewayPorts yes fail-closed）覆蓋斷言⑦。
#
# 每個拓樸驗證（缺一不可，對照 spec §11.2 / task-12-brief）：
#   ① 預設不啟用：remote status 空、無 state 檔、無 ssh/autossh 行程、--endpoint 連線失敗（SOCKET_ERROR）
#   ② 手動啟動後：agent 端 session list 有 READY、cmd submit→cmd status 得 done 且輸出正確
#   ③ serialwrapd 不重啟：daemon pid 全程不變
#   ④ close all 後乾淨復歸（同①的檢查方式）
#   ⑤ loopback 不變量：ss -ltn 綁 127.0.0.1；獨立 attacker 容器連不到
#   ⑥ trust boundary：relay 第二個本機使用者 tcp 模式可連（記錄殘留風險）、--remote-socket 模式被拒
#   ⑦ GatewayPorts yes → -R tcp 回 REMOTE_BIND_UNVERIFIED 且無殘留；--remote-socket 仍成功
#   ⑧ -L 對無對應 -R 的 relay 回 starting（非 active）
#
# docker 不可用時：印原因、exit 0（SKIP，不靜默略過）。任何斷言失敗：exit 1。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-serialwrap:remote-tunnel-test}"
SUFFIX="${SUFFIX:-$$}"

log() { echo "[serialwrap] $*"; }
fail() { echo "[serialwrap] FAIL: $*" >&2; exit 1; }

# ── docker 可用性檢查（不可用 → SKIP，非靜默略過）──
if ! command -v docker >/dev/null 2>&1; then
  log "SKIP：找不到 docker 指令，略過 remote-tunnel 三拓樸驗收"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  log "SKIP：docker daemon 不可連（docker info 失敗），略過 remote-tunnel 三拓樸驗收"
  exit 0
fi

CONTAINERS=()
NETWORKS=()

cleanup() {
  local rc=$?
  log "cleanup（containers=${#CONTAINERS[@]}, networks=${#NETWORKS[@]}）..."
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do
    docker rm -f "$c" >/dev/null 2>&1 || true
  done
  for n in ${NETWORKS[@]+"${NETWORKS[@]}"}; do
    docker network rm "$n" >/dev/null 2>&1 || true
  done
  exit "$rc"
}
trap cleanup EXIT

# ── JSON 取值（host 端 python3；避免每次 parse 都 docker run，加速）──
jpath() {  # jpath <json> <dotted.path> -> 值（純量／true／false／空字串）
  python3 -c '
import json, sys
path = sys.argv[2].split(".") if sys.argv[2] else []
cur = json.loads(sys.argv[1])
for part in path:
    if isinstance(cur, list):
        try:
            cur = cur[int(part)]
        except (ValueError, IndexError):
            cur = None
    elif isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
    if cur is None:
        break
if cur is None:
    print("")
elif isinstance(cur, bool):
    print("true" if cur else "false")
else:
    print(cur)
' "$1" "$2"
}

tunnels_count() {  # tunnels_count <remote-status-json>
  python3 -c 'import json,sys; print(len(json.loads(sys.argv[1]).get("tunnels",[])))' "$1"
}

session_ready() {  # session_ready <session-list-json> <com>
  python3 -c '
import json, sys
obj = json.loads(sys.argv[1])
com = sys.argv[2]
ok = any(s.get("com") == com and s.get("state") == "READY" for s in obj.get("sessions", []))
print("true" if ok else "false")
' "$1" "$2"
}

# ── 基本斷言 helper ──
assert_ok_true() { [[ "$(jpath "$1" ok)" == "true" ]] || fail "$2：預期 ok:true，實得：$1"; }
assert_ok_false() { [[ "$(jpath "$1" ok)" == "false" ]] || fail "$2：預期 ok:false，實得：$1"; }
assert_field_eq() {  # assert_field_eq <json> <field> <expected> <desc>
  local got; got=$(jpath "$1" "$2")
  [[ "$got" == "$3" ]] || fail "$4：欄位 $2 預期 '$3'，實得 '$got'（完整：$1）"
}

# ── docker 資源 helper ──
mk_net() {
  docker network rm "$1" >/dev/null 2>&1 || true
  docker network create "$1" >/dev/null
  NETWORKS+=("$1")
}

start_plain() {  # start_plain <name> <network>
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --init --name "$1" --network "$2" "${IMAGE_TAG}" sleep infinity >/dev/null
  CONTAINERS+=("$1")
}

start_uart() {  # start_uart <name> <network> — 容器主行程即 uart_harness.py（fake target + serialwrapd）
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --init --name "$1" --network "$2" -u tester "${IMAGE_TAG}" \
    python3 /opt/serialwrap/tools/docker/uart_harness.py >/dev/null
  CONTAINERS+=("$1")
}

sshd_up() {  # sshd_up <container> [config]
  local c=$1 cfg=${2:-/etc/ssh/sshd_config}
  docker exec "$c" bash -c "mkdir -p /run/sshd && /usr/sbin/sshd -f ${cfg}" >/dev/null
}

start_role() {  # start_role <name> <network> [gwyes] — sshd host（agent/relay）
  local name=$1 net=$2 variant=${3:-normal}
  start_plain "$name" "$net"
  local cfg=/etc/ssh/sshd_config
  [[ "$variant" == "gwyes" ]] && cfg=/etc/ssh/sshd_config.gwyes
  sshd_up "$name" "$cfg"
}

wait_uart_ready() {  # wait_uart_ready <container> [timeout_s]
  local c=$1 timeout=${2:-30} waited=0
  while (( waited < timeout )); do
    if docker exec -u tester "$c" test -f /home/tester/sw-uart.env 2>/dev/null; then
      return 0
    fi
    sleep 1; waited=$((waited + 1))
  done
  log "uart harness 逾時未就緒，$c 的 stdout："
  docker logs "$c" || true
  fail "$c：uart harness（fake target + serialwrapd）逾時未就緒"
}

teardown_now() {  # teardown_now c1 c2 ... -- net1 net2 ...（立即回收，trap 仍作最後防線）
  local containers=() networks=() seen_dash=0
  for a in "$@"; do
    if [[ "$a" == "--" ]]; then seen_dash=1; continue; fi
    if [[ "$seen_dash" == "0" ]]; then containers+=("$a"); else networks+=("$a"); fi
  done
  for c in ${containers[@]+"${containers[@]}"}; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  for n in ${networks[@]+"${networks[@]}"}; do docker network rm "$n" >/dev/null 2>&1 || true; done
}

# ── uart 端（expose／-R）呼叫封裝：daemon harness 的 RUN_DIR 是動態 tempdir，
#    每次呼叫先讀 harness 寫出的 env 檔，轉成 `docker exec -e K=V` 陣列注入 ──
UART_ENV_ARGS=()
uart_load_env() {
  local c=$1 content
  content=$(docker exec -u tester "$c" cat /home/tester/sw-uart.env 2>/dev/null) \
    || fail "$c：讀不到 sw-uart.env（harness 未就緒？）"
  UART_ENV_ARGS=()
  local line
  while IFS= read -r line; do
    [[ "$line" =~ ^export\ ([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    UART_ENV_ARGS+=("-e" "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}")
  done <<< "$content"
  [[ ${#UART_ENV_ARGS[@]} -gt 0 ]] || fail "$c：sw-uart.env 內容為空或解析失敗：$content"
}
uart_exec() {  # uart_exec <container> <serialwrap-subcmd...>
  local c=$1; shift
  uart_load_env "$c"
  docker exec -u tester "${UART_ENV_ARGS[@]}" "$c" serialwrap "$@"
}
uart_open() { local c=$1; shift; uart_exec "$c" remote "$@"; }
uart_close_all() { uart_exec "$1" remote close all; }
uart_status() { uart_exec "$1" remote status; }
uart_daemon_pid() { jpath "$(uart_exec "$1" daemon status)" pid; }
uart_state_files() {
  uart_load_env "$1"
  docker exec -u tester "${UART_ENV_ARGS[@]}" "$1" bash -c 'ls "$SERIALWRAP_RUN_DIR"/remote/*.json 2>/dev/null'
}

# ── connect 端（-L，僅拓樸 3 用）：固定 RUN_DIR，無 daemon，不需動態 env 檔 ──
ROLE_RUN_DIR=/home/tester/.sw-run
role_exec() { local c=$1; shift; docker exec -u tester -e SERIALWRAP_RUN_DIR="${ROLE_RUN_DIR}" "$c" serialwrap "$@"; }
role_open() { local c=$1; shift; role_exec "$c" remote "$@"; }
role_close_all() { role_exec "$1" remote close all; }
role_status() { role_exec "$1" remote status; }
role_state_files() {
  docker exec -u tester -e SERIALWRAP_RUN_DIR="${ROLE_RUN_DIR}" "$1" bash -c 'ls "$SERIALWRAP_RUN_DIR"/remote/*.json 2>/dev/null'
}

# ── endpoint 端（agent CLI／attacker）：純 --endpoint，與 remote CLI 的 RUN_DIR 無關 ──
endpoint_exec() {  # endpoint_exec <container> <user> <endpoint> <serialwrap-subcmd...>
  local c=$1 u=$2 ep=$3; shift 3
  docker exec -u "$u" "$c" serialwrap --endpoint "$ep" "$@"
}

assert_no_tunnel_process() {  # assert_no_tunnel_process <container> [user]
  local c=$1 u=${2:-tester} out
  out=$(docker exec -u "$u" "$c" bash -c 'pgrep -x ssh; pgrep -x autossh' 2>/dev/null || true)
  [[ -z "$out" ]] || fail "$c：仍有殘留 ssh/autossh 行程（user=$u）：$out"
}

assert_loopback_bind() {  # assert_loopback_bind <container> <port>
  local c=$1 port=$2 line addr
  line=$(docker exec "$c" ss -ltn 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p {print; exit}')
  [[ -n "$line" ]] || fail "assertion⑤：$c port $port 找不到 listener（ss -ltn 無輸出）"
  addr=$(echo "$line" | awk '{print $4}')
  case "$addr" in
    127.0.0.1:*|127.*) : ;;
    *) fail "assertion⑤：$c port $port bind 位址非 loopback：$addr" ;;
  esac
  log "  ss 確認 $c:$port 綁 $addr（loopback-only）"
}

# ── 斷言①／④共用：預設不啟用／teardown 後乾淨復歸 ──
assert_default_off() {  # assert_default_off <uart_c> <endpoint_container> <endpoint> [user]
  local uart_c=$1 epc=$2 ep=$3 user=${4:-root}
  local st n sf out ec
  st=$(uart_status "$uart_c")
  n=$(tunnels_count "$st")
  [[ "$n" == "0" ]] || fail "$uart_c：remote status 非空：$st"
  sf=$(uart_state_files "$uart_c")
  [[ -z "$sf" ]] || fail "$uart_c：發現殘留 state 檔：$sf"
  assert_no_tunnel_process "$uart_c" tester
  out=$(endpoint_exec "$epc" "$user" "$ep" daemon status)
  assert_ok_false "$out" "$epc -> $ep 應不可連"
  ec=$(jpath "$out" error_code)
  [[ "$ec" == "SOCKET_ERROR" ]] || fail "$epc -> $ep：預期 error_code=SOCKET_ERROR，實得 $ec（$out）"
  log "  default-off 確認：$uart_c 無 state／行程；$epc -> $ep 不可連（SOCKET_ERROR）"
}

assert_role_default_off() {  # assert_role_default_off <container>（-L 端自身 state，僅拓樸3用）
  local c=$1 st n sf
  st=$(role_status "$c")
  n=$(tunnels_count "$st")
  [[ "$n" == "0" ]] || fail "$c：remote status 非空：$st"
  sf=$(role_state_files "$c")
  [[ -z "$sf" ]] || fail "$c：發現殘留 state 檔：$sf"
  assert_no_tunnel_process "$c" tester
}

assert_pid_unchanged() {  # assert_pid_unchanged <uart_c> <expected_pid> <desc>
  local got; got=$(uart_daemon_pid "$1")
  [[ "$got" == "$2" ]] || fail "$3：serialwrapd pid 改變了！before=$2 after=$got"
  log "  assertion③（pid-unchanged）PASS：$1 pid=$got（$3）"
}

# ── 斷言②：agent 端 session list READY + cmd submit -> done ──
assert_session_and_cmd() {  # assert_session_and_cmd <endpoint_container> <user> <endpoint>
  local c=$1 u=$2 ep=$3 sl ready submit cmd_id status_json done stdout
  sl=$(endpoint_exec "$c" "$u" "$ep" session list)
  assert_ok_true "$sl" "session list（$c -> $ep）"
  ready=$(session_ready "$sl" "COM0")
  [[ "$ready" == "true" ]] || fail "COM0 未 READY（$c -> $ep）：$sl"

  submit=$(endpoint_exec "$c" "$u" "$ep" cmd submit --selector COM0 --cmd 'uname -a')
  assert_ok_true "$submit" "cmd submit（$c -> $ep）"
  cmd_id=$(jpath "$submit" cmd_id)
  [[ -n "$cmd_id" ]] || fail "cmd submit 未回 cmd_id（$c -> $ep）：$submit"

  status_json="" done=0
  for _ in $(seq 1 30); do
    status_json=$(endpoint_exec "$c" "$u" "$ep" cmd status --cmd-id "$cmd_id")
    if [[ "$(jpath "$status_json" command.status)" == "done" ]]; then done=1; break; fi
    sleep 1
  done
  [[ "$done" == "1" ]] || fail "cmd 逾時未完成（$c -> $ep）：$status_json"
  stdout=$(jpath "$status_json" command.stdout)
  [[ "$stdout" == "RESULT:uname -a:OK" ]] || fail "cmd stdout 不符（$c -> $ep）：得 '$stdout'"
  log "  assertion②（session+cmd）PASS：$c -> $ep（stdout=$stdout）"
}

# ══════════════════════════ 拓樸 1／direct ══════════════════════════
topology_direct() {
  log "=== 拓樸 1／direct：uart + agent 同網段（net_direct）==="
  local net="net_direct_${SUFFIX}"
  local uart="sw-rt-uart1-${SUFFIX}" agent="sw-rt-agent1-${SUFFIX}" attacker="sw-rt-attacker1-${SUFFIX}"
  local ep="tcp://127.0.0.1:7777"

  mk_net "$net"
  start_uart "$uart" "$net"
  start_role "$agent" "$net"
  start_plain "$attacker" "$net"
  wait_uart_ready "$uart"

  assert_default_off "$uart" "$agent" "$ep" root
  local pid_before; pid_before=$(uart_daemon_pid "$uart")

  local open_json; open_json=$(uart_open "$uart" "tester@${agent}:7777")
  assert_ok_true "$open_json" "拓樸1 uart 裸 remote（-R 預設）expose"
  assert_field_eq "$open_json" status active "拓樸1 uart 裸 remote（-R 預設）expose"

  assert_session_and_cmd "$agent" root "$ep"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸1 expose 後"

  # 斷言⑤：loopback 不變量 + 獨立 attacker 容器連不到
  assert_loopback_bind "$agent" 7777
  local atk_out; atk_out=$(endpoint_exec "$attacker" root "tcp://${agent}:7777" daemon status)
  assert_ok_false "$atk_out" "拓樸1 attacker 應無法連 ${agent}:7777"
  [[ "$(jpath "$atk_out" error_code)" == "SOCKET_ERROR" ]] \
    || fail "拓樸1 attacker：預期 error_code=SOCKET_ERROR，實得：$atk_out"
  log "assertion⑤（loopback+attacker 隔離）PASS：拓樸1"

  local closed; closed=$(uart_close_all "$uart")
  assert_ok_true "$closed" "拓樸1 remote close all"
  sleep 1
  assert_default_off "$uart" "$agent" "$ep" root
  assert_pid_unchanged "$uart" "$pid_before" "拓樸1 close 後（assertion④）"

  teardown_now "$uart" "$agent" "$attacker" -- "$net"
  log "=== 拓樸 1／direct：PASS ==="
}

# ══════════════════════════ 拓樸 2／NAT→host ══════════════════════════
topology_nat_host() {
  log "=== 拓樸 2／NAT→host：uart(NAT) + relay 同網段（net_a），agent CLI colocate 在 relay ==="
  local net="net_a_${SUFFIX}"
  local uart="sw-rt-uart2-${SUFFIX}" relay="sw-rt-relay2-${SUFFIX}"
  local ep="tcp://127.0.0.1:7777"
  local sock=/home/tester/.sw-relay/relay-sw.sock

  mk_net "$net"
  start_uart "$uart" "$net"
  start_role "$relay" "$net"
  wait_uart_ready "$uart"

  assert_default_off "$uart" "$relay" "$ep" root
  local pid_before; pid_before=$(uart_daemon_pid "$uart")

  # --- tcp 模式（-R 預設） ---
  local open_json; open_json=$(uart_open "$uart" -R "tester@${relay}:7777")
  assert_ok_true "$open_json" "拓樸2 tcp -R expose"
  assert_field_eq "$open_json" status active "拓樸2 tcp -R expose"
  assert_session_and_cmd "$relay" root "$ep"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸2 tcp expose 後"
  assert_loopback_bind "$relay" 7777

  # 斷言⑥ 前半：relay 上第二個本機使用者（otheruser）tcp loopback 模式「可連」（已知殘留風險）
  local other_out; other_out=$(endpoint_exec "$relay" otheruser "$ep" daemon status)
  assert_ok_true "$other_out" "assertion⑥（tcp 殘留風險）otheruser 應可連 ${relay} ${ep}"
  log "assertion⑥a（trust-boundary tcp 殘留風險，符合預期）PASS：otheruser 可連 tcp loopback"

  uart_close_all "$uart" >/dev/null
  sleep 1
  assert_default_off "$uart" "$relay" "$ep" root
  assert_pid_unchanged "$uart" "$pid_before" "拓樸2 tcp close 後"

  # --- --remote-socket 硬化模式 ---
  open_json=$(uart_open "$uart" --remote-socket "$sock" "tester@${relay}:7777")
  assert_ok_true "$open_json" "拓樸2 remote-socket expose"
  assert_field_eq "$open_json" status active "拓樸2 remote-socket expose"

  local sl ready
  sl=$(endpoint_exec "$relay" tester "$sock" session list)
  assert_ok_true "$sl" "拓樸2 remote-socket：tester session list"
  ready=$(session_ready "$sl" "COM0")
  [[ "$ready" == "true" ]] || fail "拓樸2 remote-socket：tester 應可見 COM0 READY：$sl"

  # 斷言⑥ 後半：otheruser 對 --remote-socket endpoint 應被拒（無檔案權限，.sw-relay 目錄 0700）
  local other_json; other_json=$(endpoint_exec "$relay" otheruser "$sock" session list)
  assert_ok_false "$other_json" "assertion⑥（remote-socket 硬化）otheruser 應被拒於 $sock"
  log "assertion⑥b（trust-boundary remote-socket 硬化）PASS：otheruser 被拒，tester 仍可連"

  uart_close_all "$uart" >/dev/null
  sleep 1
  docker exec "$relay" rm -f "$sock" >/dev/null 2>&1 || true
  assert_default_off "$uart" "$relay" "$ep" root
  assert_pid_unchanged "$uart" "$pid_before" "拓樸2 remote-socket close 後（assertion④）"

  teardown_now "$uart" "$relay" -- "$net"
  log "=== 拓樸 2／NAT→host：PASS ==="
}

# ══════════════════════════ 拓樸 3／NAT←client（雙 NAT relay）══════════════════════════
topology_dual_nat() {
  log "=== 拓樸 3／NAT←client：net_a=uart+relay、net_b=agent+relay，uart 與 agent 無共網段 ==="
  local neta="net_a_${SUFFIX}" netb="net_b_${SUFFIX}"
  local uart="sw-rt-uart3-${SUFFIX}" relay="sw-rt-relay3-${SUFFIX}" agent="sw-rt-agent3-${SUFFIX}"
  local ep="tcp://127.0.0.1:7777"
  local sock=/home/tester/.sw-relay/relay-sw.sock

  mk_net "$neta"
  mk_net "$netb"
  start_uart "$uart" "$neta"
  start_plain "$relay" "$neta"
  docker network connect "$netb" "$relay"
  sshd_up "$relay"
  start_plain "$agent" "$netb"
  wait_uart_ready "$uart"

  # 確認雙 NAT 隔離：uart 與 agent 互不可達
  local direct_out
  direct_out=$(docker exec "$agent" python3 -c "
import socket
s = socket.socket(); s.settimeout(2)
try:
    s.connect((\"${uart}\", 22))
    print(\"REACHABLE\")
except OSError:
    print(\"unreachable\")
" 2>/dev/null || echo "unreachable")
  [[ "$direct_out" != "REACHABLE" ]] || fail "拓樸3：uart 與 agent 竟然互通（net_a／net_b 未隔離）"

  # 斷言⑧：-L 對「無對應 -R」的 relay 應回 starting（先於 uart 開 -R 之前跑）
  local l_json
  l_json=$(role_open "$agent" -L --ready-timeout 3 "tester@${relay}:7777")
  assert_ok_true "$l_json" "拓樸3 assertion⑧：-L pre-check（無對應 -R）"
  assert_field_eq "$l_json" status starting "assertion⑧：-L 無對應 -R 時應回 starting（非 active）"
  role_close_all "$agent" >/dev/null
  sleep 1
  log "assertion⑧（-L 端到端誠實）PASS：無對應 -R 時回 starting"

  assert_default_off "$uart" "$agent" "$ep" root
  assert_role_default_off "$agent"
  local pid_before; pid_before=$(uart_daemon_pid "$uart")

  # --- tcp 模式端到端：uart -R、agent -L ---
  local r_json; r_json=$(uart_open "$uart" "tester@${relay}:7777")
  assert_ok_true "$r_json" "拓樸3 tcp -R expose"
  assert_field_eq "$r_json" status active "拓樸3 tcp -R expose"

  l_json=$(role_open "$agent" -L "tester@${relay}:7777")
  assert_ok_true "$l_json" "拓樸3 tcp -L connect"
  assert_field_eq "$l_json" status active "拓樸3 tcp -L connect"

  assert_session_and_cmd "$agent" root "$ep"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸3 tcp 端到端後"
  assert_loopback_bind "$relay" 7777

  role_close_all "$agent" >/dev/null
  uart_close_all "$uart" >/dev/null
  sleep 1
  assert_default_off "$uart" "$agent" "$ep" root
  assert_role_default_off "$agent"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸3 tcp close 後（assertion④）"

  # --- --remote-socket 硬化模式端到端（R2 finding 1：必須也走得通）---
  r_json=$(uart_open "$uart" --remote-socket "$sock" "tester@${relay}:7777")
  assert_ok_true "$r_json" "拓樸3 remote-socket -R expose"
  assert_field_eq "$r_json" status active "拓樸3 remote-socket -R expose"

  l_json=$(role_open "$agent" -L --remote-socket "$sock" "tester@${relay}:7777")
  assert_ok_true "$l_json" "拓樸3 remote-socket -L connect"
  assert_field_eq "$l_json" status active "拓樸3 remote-socket -L connect"

  assert_session_and_cmd "$agent" root "$ep"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸3 remote-socket 端到端後"

  role_close_all "$agent" >/dev/null
  uart_close_all "$uart" >/dev/null
  sleep 1
  docker exec "$relay" rm -f "$sock" >/dev/null 2>&1 || true
  assert_default_off "$uart" "$agent" "$ep" root
  assert_role_default_off "$agent"
  assert_pid_unchanged "$uart" "$pid_before" "拓樸3 remote-socket close 後（assertion④）"

  teardown_now "$uart" "$relay" "$agent" -- "$neta" "$netb"
  log "=== 拓樸 3／NAT←client：PASS ==="
}

# ══════════════════════════ 額外／斷言⑦：GatewayPorts yes fail-closed ══════════════════════════
topology_gatewayports_failclosed() {
  log "=== 額外／斷言⑦：GatewayPorts yes relay 的 fail-closed 驗證 ==="
  local net="net_gw_${SUFFIX}"
  local uart="sw-rt-uartgw-${SUFFIX}" relay="sw-rt-relaygw-${SUFFIX}"
  local sock=/home/tester/.sw-relay/gw.sock

  mk_net "$net"
  start_uart "$uart" "$net"
  start_role "$relay" "$net" gwyes
  wait_uart_ready "$uart"
  [[ "$(docker exec "$relay" grep -c '^GatewayPorts yes' /etc/ssh/sshd_config.gwyes)" == "1" ]] \
    || fail "$relay：sshd_config.gwyes 內容不符預期，前置條件不成立"

  local pid_before; pid_before=$(uart_daemon_pid "$uart")

  local tcp_json; tcp_json=$(uart_open "$uart" "tester@${relay}:7777")
  assert_ok_false "$tcp_json" "assertion⑦：tcp -R against GatewayPorts yes 應失敗"
  local ec; ec=$(jpath "$tcp_json" error_code)
  [[ "$ec" == "REMOTE_BIND_UNVERIFIED" ]] \
    || fail "assertion⑦：預期 error_code=REMOTE_BIND_UNVERIFIED，實得 $ec（$tcp_json）"

  local st n relay_ss
  st=$(uart_status "$uart")
  n=$(tunnels_count "$st")
  [[ "$n" == "0" ]] || fail "assertion⑦：fail-closed 後仍有殘留隧道：$st"
  assert_no_tunnel_process "$uart" tester
  relay_ss=$(docker exec "$relay" ss -ltn 2>/dev/null | grep ':7777' || true)
  [[ -z "$relay_ss" ]] || fail "assertion⑦：relay 仍殘留 :7777 listener：$relay_ss"
  assert_pid_unchanged "$uart" "$pid_before" "assertion⑦ tcp fail-closed 後"
  log "assertion⑦a（GatewayPorts yes → REMOTE_BIND_UNVERIFIED 無殘留）PASS"

  local sock_json; sock_json=$(uart_open "$uart" --remote-socket "$sock" "tester@${relay}:7777")
  assert_ok_true "$sock_json" "assertion⑦：--remote-socket against GatewayPorts yes 應仍成功"
  assert_field_eq "$sock_json" status active "assertion⑦：--remote-socket against GatewayPorts yes 應仍成功"
  assert_pid_unchanged "$uart" "$pid_before" "assertion⑦ remote-socket 成功後"
  log "assertion⑦b（GatewayPorts yes + --remote-socket 不受影響、仍成功）PASS"

  uart_close_all "$uart" >/dev/null
  docker exec "$relay" rm -f "$sock" >/dev/null 2>&1 || true

  teardown_now "$uart" "$relay" -- "$net"
  log "=== 額外／斷言⑦：PASS ==="
}

# ══════════════════════════ main ══════════════════════════
main() {
  log "build image: ${IMAGE_TAG}"
  DOCKER_BUILDKIT=1 docker build --progress=plain -t "${IMAGE_TAG}" "${ROOT_DIR}" || fail "docker build 失敗"

  topology_direct
  topology_nat_host
  topology_dual_nat
  topology_gatewayports_failclosed

  log "remote-tunnel acceptance: PASS"
}

main
