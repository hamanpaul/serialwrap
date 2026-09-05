"""tier `remote`：serialwrap remote 實機驗證。

第一層 rm-topo ×4：逐拓樸包裝 tools/docker/remote_tunnel_test.sh
（容器封閉世界＋假 UART，驗這台機的隧道工具鏈）；exit code＋log 尾段 →
drivers.classify_topology_run 分桶。image 建置延遲到第一個 rm-topo case。

第二層 rm-live ×3：docker 容器只當 ssh 對端，對部署 daemon＋真板驗 -R expose
穿隧道端到端／orphan 自癒／open-close 循環。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import time
from pathlib import Path

from .. import drivers
from ..harness import Case, CaseResult, register

_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "docker" / "remote_tunnel_test.sh"
_TOPO_TIMEOUT_S = 1800
_IMAGE_TAG = os.environ.get("IMAGE_TAG", "serialwrap:remote-tunnel-test")
_LIVE_PREFIX = "rhwlive"
_LIVE_PORT = 7777


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="remote", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _sweep_docker(needle: str) -> None:
    ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={needle}"],
                        capture_output=True, text=True)
    ids = [x for x in ps.stdout.split() if x]
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True)
    nets = subprocess.run(["docker", "network", "ls", "-q", "--filter", f"name={needle}"],
                          capture_output=True, text=True)
    nids = [x for x in nets.stdout.split() if x]
    if nids:
        subprocess.run(["docker", "network", "rm", *nids], capture_output=True, text=True)


def _run_topology(ctx, topo: str) -> CaseResult:
    suffix = f"rhw{os.getpid()}"
    env = dict(os.environ, SUFFIX=suffix)
    try:
        try:
            cp = subprocess.run(["bash", str(_SCRIPT), topo], capture_output=True,
                                text=True, timeout=_TOPO_TIMEOUT_S, env=env)
            rc = cp.returncode
            out = (cp.stdout or "") + "\n--- stderr ---\n" + (cp.stderr or "")
        except subprocess.TimeoutExpired as exc:
            rc = -1
            out = f"{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}\n（逾時 {_TOPO_TIMEOUT_S}s 遭終止）"
    finally:
        _sweep_docker(suffix)
    log_rel = ctx.note(f"{topo}.log", out)
    verdict, category, code, reason = drivers.classify_topology_run(rc, out[-8000:])
    return CaseResult(verdict, reason=reason or f"{topo} 拓樸驗收通過",
                      category=category, reason_code=code, evidence={"log": log_rel})


_TOPO_HINTS = (
    "包裝而非移植：斷言細節看 tools/docker/remote_tunnel_test.sh 檔頭①-⑧",
    "FAIL 行含 docker build/harness 逾時＝environment；其餘＝拓樸斷言（test）",
    "殘留容器 sw-rt-*／network net_*：script trap＋wrapper finally 雙防線",
)


@_case("rm-topo-direct", "direct：-R expose＋-L connect＋close/prune 全流程（容器封閉世界）",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_direct(ctx):
    return _run_topology(ctx, "direct")


@_case("rm-topo-nat-host", "NAT→host relay＋攻擊者容器隔離斷言",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_nat_host(ctx):
    return _run_topology(ctx, "nat_host")


@_case("rm-topo-dual-nat", "雙 NAT relay＋兩側繞行隔離斷言",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_dual_nat(ctx):
    return _run_topology(ctx, "dual_nat")


@_case("rm-topo-agent-pull", "方向反轉：agent 端 -L 拉 uart 的 serialwrapd.sock（無 -R、無 relay）",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_agent_pull(ctx):
    return _run_topology(ctx, "agent_pull")


@_case("rm-topo-gwports", "GatewayPorts/--remote-socket fail-closed＋teardown 複查",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_gwports(ctx):
    return _run_topology(ctx, "gwports")


_LIVE_HINTS = (
    "受測物＝部署 daemon＋remote CLI＋真板；容器只是 sshd 對端",
    "host→容器 ssh 用 image 預燒 tester 金鑰（docker cp 匯出）＋--ssh-opt 關 host-key 檢查",
    "close 後淨空＝remote status 空＋state dir 無 *.json/cm-*/*.log＋無殘留 ssh 行程",
    "daemon pid 全程不變＝remote 純 CLI 便利層、daemon 零觸碰",
)


def _ensure_image(ctx) -> str | None:
    chk = subprocess.run(["docker", "image", "inspect", _IMAGE_TAG],
                         capture_output=True, text=True)
    if chk.returncode == 0:
        return None
    root = Path(__file__).resolve().parents[2]
    bld = subprocess.run(["docker", "build", "-t", _IMAGE_TAG, str(root)],
                         capture_output=True, text=True, timeout=1800)
    ctx.note("image-build.log", (bld.stdout or "") + "\n" + (bld.stderr or ""))
    if bld.returncode != 0:
        return f"docker build 失敗 rc={bld.returncode}"
    return None


def _start_ssh_peer(ctx, tag: str) -> tuple[str, str, Path | None]:
    name = f"{_LIVE_PREFIX}-{tag}-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    run = subprocess.run(["docker", "run", "-d", "--init", "--name", name, _IMAGE_TAG,
                          "sleep", "infinity"], capture_output=True, text=True)
    if run.returncode != 0:
        return name, "", None
    subprocess.run(["docker", "exec", name, "bash", "-c",
                    "mkdir -p /run/sshd && /usr/sbin/sshd"], capture_output=True, text=True)
    if subprocess.run(["docker", "exec", name, "pgrep", "-x", "sshd"],
                      capture_output=True, text=True).returncode != 0:
        return name, "", None
    ip = subprocess.run(["docker", "inspect", "-f",
                         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                        capture_output=True, text=True).stdout.strip()
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    key = ctx.case_dir / "id_ed25519"
    temp_key = f"/tmp/{name}-id_ed25519"
    exported = subprocess.run(
        ["docker", "exec", "-u", "tester", name, "bash", "-lc",
         f"install -m 600 ~/.ssh/id_ed25519 {temp_key}"],
        capture_output=True,
        text=True,
    )
    if exported.returncode != 0:
        return name, "", None
    try:
        cp = subprocess.run(["docker", "cp", f"{name}:{temp_key}", str(key)],
                            capture_output=True, text=True)
    finally:
        subprocess.run(["docker", "exec", "-u", "tester", name, "rm", "-f", temp_key],
                       capture_output=True, text=True)
    if cp.returncode != 0 or not key.exists():
        return name, "", None
    key.chmod(0o600)
    return name, ip, key


def _remote_open(ctx, ip: str, key: Path) -> dict:
    return ctx.sw.run("remote", f"tester@{ip}:{_LIVE_PORT}",
                      f"--ssh-opt=-i{key}",
                      "--ssh-opt=-oStrictHostKeyChecking=no",
                      "--ssh-opt=-oUserKnownHostsFile=/dev/null",
                      timeout=60)


def _agent_exec(name: str, *sub: str, timeout: float = 60.0) -> dict:
    cp = subprocess.run(["docker", "exec", "-u", "tester", name, "serialwrap",
                         "--endpoint", f"tcp://127.0.0.1:{_LIVE_PORT}", *sub],
                        capture_output=True, text=True, timeout=timeout)
    out = cp.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"_raw": out}
    data["_rc"] = cp.returncode
    data["_stderr"] = cp.stderr.strip()
    return data


def _registry_leftovers() -> list[str]:
    d = drivers.remote_state_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.suffix in (".json", ".log") or p.name.startswith("cm-"))


def _live_teardown(ctx, name: str) -> None:
    ctx.sw.run("remote", "close", "all")
    if name:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


@_case("rm-live-e2e", "-R expose 至容器→穿隧道 session list＋真板 marker＋WAL 歸因→close 淨空、daemon pid 不變",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_e2e(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    start_seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "e2e")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        opened = _remote_open(ctx, ip, key)
        ctx.note("open.json", str(opened))
        if not opened.get("ok") or opened.get("status") != "active":
            return CaseResult("FAIL",
                              reason=f"remote -R expose 未 active（{opened.get('error_code') or opened.get('status')}）",
                              category="test", reason_code="tunnel_open_failed")
        sl = _agent_exec(name, "session", "list")
        ctx.note("session-list.json", str(sl))
        if not sl.get("ok") or not any(s.get("com") == "COM0" and s.get("state") == "READY"
                                       for s in sl.get("sessions") or []):
            return CaseResult("FAIL", reason="容器內穿隧道 session list 未見 COM0 READY",
                              category="test", reason_code="tunnel_session_list_failed")
        marker = f"RMLIVE_{random.randint(10000, 99999)}"
        sub = _agent_exec(name, "cmd", "submit", "--selector", "COM0",
                          "--cmd", f"echo {marker}", "--source", "agent:rhwremote",
                          "--cmd-timeout", "12")
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult("FAIL", reason=f"穿隧道 cmd submit 未回 cmd_id（{sub.get('error_code')}）",
                              category="test", reason_code="tunnel_submit_failed")
        command: dict = {}
        deadline = time.monotonic() + 30
        time.sleep(1.5)
        while time.monotonic() < deadline:
            command = _agent_exec(name, "cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                break
            time.sleep(0.5)
        ctx.note("cmd.json", str(command))
        if command.get("status") != "done" or marker not in (command.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"真板未回 marker（status={command.get('status')}）",
                              category="test", reason_code="tunnel_cmd_failed")
        exp = ctx.sw.run("wal", "export", "--from-seq", str(start_seq))
        tx = [r for r in exp.get("records") or []
              if r.get("dir") == "TX" and r.get("source") == "agent:rhwremote"]
        ctx.note("wal-tx.json", str(tx))
        if not tx:
            return CaseResult("FAIL", reason="WAL 無 source=agent:rhwremote 的 TX 記錄（穿隧道歸因遺失）",
                              category="test", reason_code="wal_source_attribution_lost")
        closed = ctx.sw.run("remote", "close", "all")
        ctx.note("close.json", str(closed))
        time.sleep(1)
        st = ctx.sw.run("remote", "status")
        if st.get("tunnels"):
            return CaseResult("FAIL", reason=f"close all 後 remote status 非空：{st.get('tunnels')}",
                              category="test", reason_code="tunnel_state_leak")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"close all 後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_state_leak")
        orphan = subprocess.run(["pgrep", "-af", f"ssh.*{ip}"],
                                capture_output=True, text=True).stdout.strip()
        if orphan:
            return CaseResult("FAIL", reason=f"close all 後殘留 ssh 行程：{orphan}",
                              category="test", reason_code="tunnel_orphan_ssh")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)


@_case("rm-live-orphan", "kill -9 隧道 ssh→remote status prune 自癒→重開成功",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_orphan(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "orphan")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        opened = _remote_open(ctx, ip, key)
        ctx.note("open.json", str(opened))
        ssh_pid = opened.get("pid")
        if not opened.get("ok") or opened.get("status") != "active" or not ssh_pid:
            return CaseResult("FAIL",
                              reason=f"remote -R expose 未 active（{opened.get('error_code') or opened.get('status')}）",
                              category="test", reason_code="tunnel_open_failed")
        os.kill(int(ssh_pid), 9)
        time.sleep(1)
        st = ctx.sw.run("remote", "status")
        ctx.note("status-after-kill.json", str(st))
        if st.get("tunnels"):
            return CaseResult("FAIL", reason=f"kill -9 後 remote status 未 prune：{st.get('tunnels')}",
                              category="test", reason_code="tunnel_prune_failed")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"prune 後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_prune_failed")
        reopened = _remote_open(ctx, ip, key)
        ctx.note("reopen.json", str(reopened))
        if not reopened.get("ok") or reopened.get("status") != "active":
            return CaseResult("FAIL",
                              reason=f"prune 後重開失敗（{reopened.get('error_code') or reopened.get('status')}）",
                              category="test", reason_code="tunnel_reopen_failed")
        ctx.sw.run("remote", "close", "all")
        time.sleep(1)
        if ctx.sw.run("remote", "status").get("tunnels") or _registry_leftovers():
            return CaseResult("FAIL", reason="收尾 close all 後仍有殘留",
                              category="test", reason_code="tunnel_state_leak")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)


@_case("rm-live-cycle", "open/close ×5 registry 不累積、daemon 零觸碰",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_cycle(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "cycle")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        for i in range(5):
            opened = _remote_open(ctx, ip, key)
            if not opened.get("ok") or opened.get("status") != "active":
                ctx.note(f"round{i}-open.json", str(opened))
                return CaseResult("FAIL",
                                  reason=f"第 {i + 1} 輪 open 未 active（{opened.get('error_code') or opened.get('status')}）",
                                  category="test", reason_code="tunnel_open_failed")
            st = ctx.sw.run("remote", "status")
            if len(st.get("tunnels") or []) != 1:
                return CaseResult("FAIL", reason=f"第 {i + 1} 輪 status 隧道數≠1：{st.get('tunnels')}",
                                  category="test", reason_code="tunnel_state_leak")
            ctx.sw.run("remote", "close", "all")
            time.sleep(1)
            st = ctx.sw.run("remote", "status")
            if st.get("tunnels"):
                return CaseResult("FAIL", reason=f"第 {i + 1} 輪 close 後 status 非空：{st.get('tunnels')}",
                                  category="test", reason_code="tunnel_state_leak")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"5 輪後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_state_leak")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)
