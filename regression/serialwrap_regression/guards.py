"""護欄：U-Boot 唯讀白名單、收尾 READY 保證、throwaway daemon 隔離。

U-Boot 下一個手滑就是持久化（saveenv/setenv/flash 寫入），禁令由本層強制、
不靠 case 自律；ThrowawayDaemon 讓帳密失敗情境完全不碰 prod 組態。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any


class UBootGuardError(RuntimeError):
    """U-Boot 唯讀護欄攔截（禁令命令或非法離開方式）。"""


UBOOT_RO_WHITELIST: tuple[str, ...] = ("printenv", "bdinfo", "version", "help", "echo")
LEAVE_ALLOWED: tuple[str, ...] = ("boot", "reset")
_FORBIDDEN = re.compile(
    r"\b(saveenv|env\s+save|env\s+default|setenv|sf\s+write|nand\s+write|mmc\s+write|tftpboot)\b"
)
_UBOOT_PROMPT = re.compile(r"(=>|U-Boot>)\s*$", re.MULTILINE)


def validate_uboot_cmd(cmd: str) -> None:
    """唯讀白名單驗證：首 token 必在白名單、全串不得命中禁令、禁串接（; 換行 && |）。"""
    text = cmd.strip()
    if not text:
        raise UBootGuardError("空命令")
    if any(sep in text for sep in (";", "\n", "&&", "||", "|")):
        raise UBootGuardError(f"禁止串接／多行命令：{cmd!r}")
    if _FORBIDDEN.search(text):
        raise UBootGuardError(f"U-Boot 禁令命令（會持久化／燒寫）：{cmd!r}")
    first = text.split()[0]
    if first not in UBOOT_RO_WHITELIST:
        raise UBootGuardError(f"非白名單 U-Boot 命令：{first!r}（允許：{UBOOT_RO_WHITELIST}）")


class UBootConsole:
    """經 tmux 內 console（serialwrap-minicom）與 U-Boot 互動；只暴露唯讀操作。

    建構不做 I/O（可單測 leave 驗證）；互動皆走 ctx.tmux 對 tmux_session 送鍵／capture。
    """

    def __init__(self, ctx: Any, com: str, tmux_session: str) -> None:
        self._ctx = ctx
        self._com = com
        self._ses = tmux_session

    def _capture(self) -> str:
        from realhw.drivers import strip_ansi

        return strip_ansi(self._ctx.tmux.capture(self._ses))

    def at_prompt(self) -> bool:
        return bool(_UBOOT_PROMPT.search(self._capture()))

    def interrupt_autoboot(self, window_s: float = 15.0) -> bool:
        """週期送鍵直到出現 U-Boot prompt；autoboot 窗僅 3s，需在 reboot 後立即呼叫。"""
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            self._ctx.tmux.send(self._ses, "", enter=True)  # 任意鍵（Enter）停 autoboot
            time.sleep(0.3)
            if self.at_prompt():
                return True
        return False

    def readonly_cmd(self, cmd: str, settle_s: float = 1.5) -> str:
        """白名單驗證後送出，回傳 capture 文字；非白名單一律 raise、bytes 不落 UART。"""
        validate_uboot_cmd(cmd)
        self._ctx.tmux.send(self._ses, cmd, enter=True)
        time.sleep(settle_s)
        return self._capture()

    def leave(self, via: str = "boot") -> None:
        """以 boot／reset 離開 U-Boot（讓板子正常開完機）；其他方式 raise。"""
        if via not in LEAVE_ALLOWED:
            raise UBootGuardError(f"非法離開方式：{via!r}（允許：{LEAVE_ALLOWED}）")
        self._ctx.tmux.send(self._ses, via, enter=True)


def ensure_ready(ctx: Any, com: str, *, timeout_s: float, recover: bool = True) -> bool:
    """收尾保證：等 session 回 READY；逾時且 recover 時依狀態語意恢復一次再等一輪。"""
    if ctx.sw.wait_state(com, "READY", timeout_s=timeout_s):
        return True
    if not recover:
        return False
    from realhw.harness import recovery_command

    state = ctx.sw.session(com).get("state")
    ctx.sw.run(*recovery_command(state), "--selector", com)
    return ctx.sw.wait_state(com, "READY", timeout_s=timeout_s)


# throwaway daemon 覆寫的目錄變數（對齊 tests/conftest.py 的隔離維度子集）。
# BY_PATH 必須一併沙盒化（round 4 實測）：DeviceWatcher 的 extra_scan_dirs 掃 by-path，
# 漏覆寫會讓 throwaway 看到主機真實 /dev/serial/by-path、間歇把 PROD 在用的裝置撈進
# 偵測池並對其並行探測（two-reader 風險）。
_TA_ENV_DIRS: tuple[str, ...] = (
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_WAL_DIR",
    "SERIALWRAP_BY_ID_DIR",
    "SERIALWRAP_BY_PATH_DIR",
    "SERIALWRAP_CONFIG_DIR",
    "SERIALWRAP_PROFILE_DIR",
)


def throwaway_env(workdir: Path, by_id_dir: Path, run_dir: Path) -> dict[str, str]:
    """組 throwaway daemon 的隔離 env（純函式）：state/wal/config/profiles 壓進 workdir、
    by-id 指 sandbox；**RUN_DIR 必須是獨立短路徑**——socket 落在 RUN_DIR 下，AF_UNIX
    `sun_path` 上限 107 字元，workdir 巢狀在 report 目錄樹（`~/b-log/regression-reports/
    tp-<ts>/<case-id>/ta/`）下必然超限（首輪實測 108/111 字元必現 OSError）。
    """
    env = dict(os.environ)
    mapping = {
        "SERIALWRAP_RUN_DIR": run_dir,
        "SERIALWRAP_STATE_DIR": workdir / "state",
        "SERIALWRAP_WAL_DIR": workdir / "wal",
        "SERIALWRAP_BY_ID_DIR": by_id_dir,
        "SERIALWRAP_BY_PATH_DIR": workdir / "bypath",  # 空目錄＝主機 by-path 對 throwaway 不可見
        "SERIALWRAP_CONFIG_DIR": workdir / "config",
        "SERIALWRAP_PROFILE_DIR": workdir / "profiles",
    }
    for key, path in mapping.items():
        env[key] = str(path)
    env.pop("SERIALWRAP_ENDPOINT", None)  # 由 RUN_DIR 推導，不繼承外層
    return env


class ThrowawayDaemon:
    """throwaway serialwrapd context manager：獨立 XDG/socket/by-id sandbox，prod 零接觸。

    背景啟動必須用純 ``nohup &``（其餘手法會 exit 144，見 CLAUDE.md 實證）；
    離場 kill daemon、保留 workdir 當 evidence。
    """

    def __init__(self, exe: str, workdir: Path, by_id_dir: Path, profile_yaml: str) -> None:
        self._exe = exe
        self._workdir = Path(workdir)
        self._by_id_dir = Path(by_id_dir)
        self._profile_yaml = profile_yaml
        self._env: dict[str, str] = {}
        self._proc_pgid: int | None = None
        self._run_dir: Path | None = None

    def __enter__(self) -> "ThrowawayDaemon":
        import tempfile

        for sub in ("state", "wal", "config", "profiles", "bypath"):
            (self._workdir / sub).mkdir(parents=True, exist_ok=True)
        self._by_id_dir.mkdir(parents=True, exist_ok=True)
        (self._workdir / "profiles" / "default.yaml").write_text(
            self._profile_yaml, encoding="utf-8"
        )
        # RUN_DIR 用 /tmp 下短路徑（socket 的 AF_UNIX 107 字元上限，見 throwaway_env docstring）。
        self._run_dir = Path(tempfile.mkdtemp(prefix="swreg-ta-", dir="/tmp"))
        self._env = throwaway_env(self._workdir, self._by_id_dir, self._run_dir)
        daemon_exe = str(Path(self._exe).with_name("serialwrapd"))
        log = open(self._workdir / "daemon.log", "ab")
        proc = subprocess.Popen(
            ["nohup", daemon_exe],
            env=self._env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._proc_pgid = proc.pid
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.run("daemon", "status").get("ok"):
                    return self
                time.sleep(1.0)
        except BaseException:
            # 任何非預期例外（如 subprocess.TimeoutExpired）都不得洩漏 daemon 子行程。
            self.__exit__(None, None, None)
            raise
        self.__exit__(None, None, None)
        raise RuntimeError(f"throwaway daemon 未在 20s 內就緒（log：{self._workdir}/daemon.log）")

    def run(self, *args: str, timeout: float = 30.0) -> dict:
        """以 throwaway env 呼叫 pinned CLI（JSON 解析同 SwCli 慣例）。"""
        import json

        cp = subprocess.run(
            [self._exe, *args], env=self._env, capture_output=True, text=True, timeout=timeout
        )
        out = (cp.stdout or "").strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"_raw": out}
        data["_rc"] = cp.returncode
        data["_stderr"] = (cp.stderr or "").strip()
        return data

    def wal_text(self) -> str:
        """讀 sandbox WAL 全文（evidence／TX 行為斷言用）。"""
        parts = []
        wal_dir = self._workdir / "wal"
        for p in sorted(wal_dir.glob("*")):
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(parts)

    def __exit__(self, *exc: Any) -> None:
        self.run("daemon", "stop", timeout=15)
        if self._proc_pgid:
            try:
                os.killpg(self._proc_pgid, 15)
            except (ProcessLookupError, PermissionError):
                pass
        # workdir（state/wal/daemon.log）保留當 evidence；短路徑 run dir 清掉不留 socket。
        if self._run_dir is not None:
            import shutil

            shutil.rmtree(self._run_dir, ignore_errors=True)


def sh_quote(text: str) -> str:
    """shell 安全引用（case 組長命令用）。"""
    return shlex.quote(text)
