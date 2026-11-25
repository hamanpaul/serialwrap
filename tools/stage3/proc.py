"""子程序執行與程序句柄（stdlib only）。

`run_cmd` 為唯一的同步執行入口；逾時回傳 `rc=-1` 與 `timed_out=True`，不丟例外。
`spawn` 啟動背景程序，stdout／stderr 導向 cwd 下的 `spawn-<pid>.log`。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CmdResult:
    rc: int            # timeout 時為 -1
    stdout: str | bytes
    stderr: str | bytes
    elapsed_s: float
    timed_out: bool


Runner = Callable[..., CmdResult]   # run(argv, *, env=None, timeout=None, cwd=None, bytes_output=False) -> CmdResult


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_cmd(argv: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None,
            cwd: Path | None = None, bytes_output: bool = False) -> CmdResult:
    """同步執行命令並擷取 stdout／stderr；逾時回傳 `rc=-1`、`timed_out=True`。

    預設維持 UTF-8 文字輸出；`bytes_output=True` 時保留 stdout／stderr 原始位元組，
    適用於以 NUL 分隔且可能含換行字元的 Git 路徑清單。
    """
    start = time.monotonic()
    text_options = {} if bytes_output else {"encoding": "utf-8", "errors": "replace"}
    try:
        cp = subprocess.run(
            list(argv),
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            capture_output=True,
            **text_options,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or (b"" if bytes_output else "")
        stderr = exc.stderr or (b"" if bytes_output else "")
        if not bytes_output:
            stdout, stderr = _text(stdout), _text(stderr)
        return CmdResult(rc=-1, stdout=stdout, stderr=stderr,
                         elapsed_s=time.monotonic() - start, timed_out=True)
    empty = b"" if bytes_output else ""
    return CmdResult(rc=cp.returncode, stdout=cp.stdout or empty, stderr=cp.stderr or empty,
                     elapsed_s=time.monotonic() - start, timed_out=False)


def run_json(run: Runner, argv: Sequence[str], **kw: Any) -> tuple[CmdResult, dict | None]:
    """執行命令並把 stdout 解析為 JSON object；非 object 或解析失敗回傳 `None`。"""
    res = run(argv, **kw)
    try:
        doc = json.loads(res.stdout)
    except (ValueError, RecursionError, TypeError):   # JSONDecodeError 為 ValueError 子類；另含整數位數上限與過深巢狀
        return res, None
    return res, doc if isinstance(doc, dict) else None


class ProcHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Spawner = Callable[..., ProcHandle]   # spawn(argv, *, env=None, cwd=None) -> ProcHandle


class _SpawnedProc:
    """包住 Popen；輸出由背景執行緒寫入 `spawn-<pid>.log`，避免管線塞滿。

    寫檔失敗（磁碟滿、檔案被關閉等）只記錄於 `log_error`，之後仍持續排空 stdout，
    子程序不會因管線塞滿而卡住；觀察者可依 `log_error` 判定輸出是否完整。
    """

    def __init__(self, popen: subprocess.Popen, log_path: Path) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.log_path = log_path
        self.log_error: str | None = None
        try:
            self._log = log_path.open("wb")          # 在建構當下同步開檔：失敗要直接丟例外，不可留下無人讀取的管線
        except OSError:
            popen.kill()
            popen.wait()
            if popen.stdout is not None:
                popen.stdout.close()
            raise
        self._pump = threading.Thread(target=self._copy_output, daemon=True)
        self._pump.start()

    def _record_error(self, exc: BaseException) -> None:
        if self.log_error is None:                 # 只保留第一個錯誤，後續錯誤不覆寫
            self.log_error = f"{type(exc).__name__}: {exc}"

    def _copy_output(self) -> None:
        stream = self._popen.stdout
        try:
            if stream is None:
                return
            failed = False
            for chunk in iter(lambda: stream.read(4096), b""):
                if failed:                         # 寫檔已失敗：只排空、不再寫，避免子程序卡在滿的管線
                    continue
                try:
                    self._log.write(chunk)
                    self._log.flush()
                except (OSError, ValueError) as exc:
                    self._record_error(exc)
                    failed = True
        except (OSError, ValueError) as exc:       # 讀取端異常（管線被關閉等）
            self._record_error(exc)
        finally:
            try:
                self._log.close()
            except (OSError, ValueError) as exc:
                self._record_error(exc)

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()

    def wait(self, timeout: float | None = None) -> int:
        rc = self._popen.wait(timeout=timeout)
        self._pump.join(timeout=5)
        return rc


def spawn(argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> ProcHandle:
    """啟動背景程序；輸出寫入工作目錄下的 `spawn-<pid>.log`。"""
    base = Path(cwd) if cwd is not None else Path.cwd()
    popen = subprocess.Popen(
        list(argv),
        env=dict(env) if env is not None else None,
        cwd=str(base),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return _SpawnedProc(popen, base / f"spawn-{popen.pid}.log")
