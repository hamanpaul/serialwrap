"""bridge 的方向 worker、stderr 排空與程序狀態監看執行緒。"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain

from .dut_process import DutStdinClosed

if TYPE_CHECKING:
    from .core import Bridge


def tx_reader(bridge: Bridge) -> None:
    """從端點讀取 serialwrap TX，先留證據再放入有界佇列。"""
    try:
        while not bridge._stop_event.is_set():
            if bridge._pump_checkpoint("tx_reader", "TX", "reader"):
                return
            if bridge._closing:
                return
            if bridge._is_transport_disconnected():
                bridge._stop_event.wait(0.01)
                continue
            bridge._reader_io_now("TX", True)
            try:
                data = bridge.endpoint.read(bridge.cfg.read_chunk, 0.05)
            except OSError as exc:
                bridge._reader_io_now("TX", False)
                if bridge._is_transport_disconnected():
                    continue
                if bridge._stop_event.is_set():
                    return
                bridge._record_meta({"event": "io_error", "observation": "serial_read", "error": str(exc)})
                bridge._fail("HARNESS_ENDPOINT_IO_ERROR", exc)
                return
            try:
                if not data:
                    continue
                bridge._note_activity("serial_read")
                if not bridge._record_data("serial_read", data):
                    return
                if not bridge.tx_q.put(data, stop=bridge._stop_event):
                    return
            finally:
                bridge._reader_io_now("TX", False)
    finally:
        bridge._pump_stopped("tx_reader")


def tx_writer(bridge: Bridge) -> None:
    """將 TX 佇列寫入 DUT stdin，只記錄每次實際接受的 bytes。"""
    try:
        drop_every_1000th = (
            getattr(bridge.cfg, "test_overrides", {}).get("UART_STDIO_BRIDGE_TEST_FAULT") == "drop_every_1000th"
        )
        total_tx_bytes = 0
        while not bridge._stop_event.is_set():
            if bridge._pump_checkpoint("tx_writer", "TX", "writer"):
                return
            data = bridge.tx_q.get(0.05)
            if data is None:
                continue
            offset = 0
            bridge._pump_busy_now("tx_writer", True)
            bridge._pump_inflight_now("tx_writer", len(data))
            try:
                while offset < len(data) and not bridge._stop_event.is_set():
                    if bridge._pump_checkpoint("tx_writer", "TX", "writer"):
                        return
                    if bridge._should_discard_tx_remainder(bridge._process_generation()):
                        offset = len(data)
                        break
                    bridge._pump_inflight_now("tx_writer", len(data) - offset)

                    if drop_every_1000th:
                        if total_tx_bytes % 1000 == 999:
                            # 丟棄轉送給 DUT 的第 1000 個 byte，但照常記錄於 wire
                            dropped_byte = data[offset : offset + 1]
                            bridge._note_activity("dut_stdin_write")
                            if not bridge._record_data("dut_stdin_write", dropped_byte):
                                return
                            offset += 1
                            total_tx_bytes += 1
                            bridge._pump_inflight_now("tx_writer", len(data) - offset)
                            continue
                        bytes_until_drop = 999 - (total_tx_bytes % 1000)
                        to_write = data[offset : offset + min(len(data) - offset, bytes_until_drop)]
                    else:
                        to_write = data[offset:]

                    try:
                        accepted = bridge.dut.stdin_write_some(to_write)
                    except OSError as exc:
                        if isinstance(exc, DutStdinClosed):
                            process_generation = bridge._process_generation()
                            if not (
                                bridge.cfg.allow_dut_exit
                                or bridge._is_intentional_dut_exit(process_generation)
                            ):
                                if not bridge._stop_event.is_set():
                                    bridge._record_meta(
                                        {"event": "io_error", "observation": "dut_stdin_write", "error": str(exc)}
                                    )
                                    bridge._fail("HARNESS_DUT_EXITED", exc)
                                return
                            logical_generation = bridge._dut_generation()
                            while not bridge._stop_event.is_set():
                                if bridge._pump_checkpoint("tx_writer", "TX", "writer"):
                                    return
                                if bridge._dut_generation() != logical_generation:
                                    break
                                bridge._wait_for_dut_generation_change(logical_generation, timeout_s=0.05)
                            continue
                        else:
                            if not bridge._stop_event.is_set():
                                bridge._record_meta(
                                    {"event": "io_error", "observation": "dut_stdin_write", "error": str(exc)}
                                )
                                bridge._fail("HARNESS_DUT_EXITED", exc)
                            return
                    if accepted < 0 or accepted > len(to_write):
                        bridge._fail("HARNESS_DUT_EXITED", "DUT 回報無效寫入長度")
                        return
                    if accepted == 0:
                        bridge._stop_event.wait(0.01)
                        continue
                    payload = data[offset : offset + accepted]
                    bridge._note_activity("dut_stdin_write")
                    if not bridge._record_data("dut_stdin_write", payload):
                        return
                    offset += accepted
                    total_tx_bytes += accepted
                    bridge._pump_inflight_now("tx_writer", len(data) - offset)
            finally:
                bridge._pump_inflight_now("tx_writer", 0)
                bridge._pump_busy_now("tx_writer", False)
    finally:
        bridge._pump_stopped("tx_writer")


def rx_reader(bridge: Bridge) -> None:
    """讀取 DUT stdout；EOF 留 META，並等候下一個 DUT 世代。"""
    try:
        while not bridge._stop_event.is_set():
            if bridge._pump_checkpoint("rx_reader", "RX", "reader"):
                return
            if bridge._closing:
                return
            generation = bridge._dut_generation()
            bridge._reader_io_now("RX", True)
            try:
                try:
                    data = bridge.dut.stdout_read(bridge.cfg.read_chunk, timeout_s=0.05)
                except TypeError:
                    data = bridge.dut.stdout_read(bridge.cfg.read_chunk)
            except OSError as exc:
                bridge._reader_io_now("RX", False)
                if bridge._stop_event.is_set():
                    return
                bridge._record_meta({"event": "io_error", "observation": "dut_stdout_read", "error": str(exc)})
                bridge._fail("HARNESS_DUT_EXITED", exc)
                return
            try:
                if data is None:
                    continue
                if not data and bridge._closing:
                    return
                if data:
                    bridge._note_activity("dut_stdout_read")
                    if not bridge._record_data("dut_stdout_read", data):
                        return
                    if not bridge.rx_q.put(data, stop=bridge._stop_event):
                        return
                    continue
            finally:
                bridge._reader_io_now("RX", False)
            bridge._record_stdout_eof(generation)
            if not bridge._wait_for_dut_generation_change(generation):
                return
    finally:
        bridge._pump_stopped("rx_reader")


def rx_writer(bridge: Bridge) -> None:
    """將 DUT stdout 寫到端點；不確定接受長度時立即 fail-stop。"""
    try:
        while not bridge._stop_event.is_set():
            if bridge._pump_checkpoint("rx_writer", "RX", "writer"):
                return
            if bridge._closing:
                try:
                    peer_open = bridge.endpoint.peer_open()
                except OSError:
                    peer_open = None
                if peer_open is False:
                    bridge.rx_q.discard_all()
                    return
            data = bridge.rx_q.get(0.05)
            if data is None:
                continue
            offset = 0
            bridge._pump_busy_now("rx_writer", True)
            bridge._pump_inflight_now("rx_writer", len(data))
            endpoint_generation = bridge._endpoint_generation()
            try:
                while offset < len(data) and not bridge._stop_event.is_set():
                    if bridge._is_transport_disconnected():
                        discarded = len(data) - offset
                        bridge._record_meta(
                            {
                                "event": "fault",
                                "action_id": uuid.uuid4().hex,
                                "action": "transport_disconnect",
                                "discarded_len": discarded,
                            },
                            loss_flag=False,
                        )
                        offset = len(data)
                        break
                    if bridge._endpoint_generation() != endpoint_generation:
                        discarded = len(data) - offset
                        bridge._record_meta(
                            {
                                "event": "fault",
                                "action_id": uuid.uuid4().hex,
                                "action": "transport_disconnect",
                                "discarded_len": discarded,
                            },
                            loss_flag=False,
                        )
                        offset = len(data)
                        break
                    if bridge._pump_checkpoint("rx_writer", "RX", "writer"):
                        return
                    bridge._pump_inflight_now("rx_writer", len(data) - offset)
                    try:
                        accepted = bridge.endpoint.write_some(
                            data[offset:], bridge.cfg.write_timeout_s
                        )
                    except EndpointWriteUncertain as exc:
                        requested = int(getattr(exc, "requested_len", len(data) - offset))
                        bridge._record_meta(
                            {
                                "event": "write_uncertain",
                                "observation": "serial_write",
                                "offset_start": bridge._offset("serial_write"),
                                "requested_len": requested,
                                "accepted_len": None,
                                "error": str(exc),
                            },
                            loss_flag=True,
                        )
                        bridge._fail("HARNESS_WRITE_UNCERTAIN", exc)
                        return
                    except OSError as exc:
                        if bridge._is_transport_disconnected():
                            discarded = len(data) - offset
                            bridge._record_meta(
                                {
                                    "event": "fault",
                                    "action_id": uuid.uuid4().hex,
                                    "action": "transport_disconnect",
                                    "discarded_len": discarded,
                                },
                                loss_flag=False,
                            )
                            offset = len(data)
                            break
                        if bridge._stop_event.is_set():
                            return
                        bridge._record_meta({"event": "io_error", "observation": "serial_write", "error": str(exc)})
                        bridge._fail("HARNESS_ENDPOINT_IO_ERROR", exc)
                        return
                    if accepted < 0 or accepted > len(data) - offset:
                        bridge._fail("HARNESS_ENDPOINT_IO_ERROR", "端點回報無效寫入長度")
                        return
                    if accepted == 0:
                        if bridge._closing:
                            try:
                                peer_open = bridge.endpoint.peer_open()
                            except OSError:
                                peer_open = None
                            if peer_open is False:
                                bridge.rx_q.discard_all()
                                break
                        bridge._stop_event.wait(0.01)
                        continue
                    payload = data[offset : offset + accepted]
                    bridge._note_activity("serial_write")
                    if not bridge._record_data("serial_write", payload):
                        return
                    offset += accepted
                    bridge._pump_inflight_now("rx_writer", len(data) - offset)
            finally:
                bridge._pump_inflight_now("rx_writer", 0)
                bridge._pump_busy_now("rx_writer", False)
    finally:
        bridge._pump_stopped("rx_writer")


def stderr_drain(bridge: Bridge) -> None:
    """持續排空 DUT stderr；達上限後只計數、不再寫入檔案。"""
    try:
        while not bridge._stop_event.is_set():
            try:
                data = bridge.dut.stderr_read(65536)
            except OSError as exc:
                if not bridge._stop_event.is_set():
                    bridge._record_meta({"event": "io_error", "observation": "dut_stderr_read", "error": str(exc)})
                    bridge._fail("HARNESS_DUT_EXITED", exc)
                return
            if not data:
                generation = bridge._dut_generation()
                if not bridge._wait_for_dut_generation_change(generation, timeout_s=0.05):
                    if bridge._stop_event.wait(0.01):
                        return
                continue
            bridge._write_stderr(data)
    finally:
        bridge._pump_stopped("stderr_drain")


def dut_watch(bridge: Bridge) -> None:
    """每 100 ms 監看 DUT；非預期離開即 fail-stop。"""
    logged_generation = 0
    try:
        while not bridge._closing and not bridge._stop_event.wait(0.1):
            bridge._check_writer_failure()
            generation = bridge._process_generation()
            try:
                returncode = bridge.dut.poll()
            except Exception as exc:
                if not bridge._stop_event.is_set():
                    bridge._record_meta({"event": "io_error", "observation": "dut_poll", "error": str(exc)})
                    bridge._fail("HARNESS_DUT_EXITED", exc)
                return
            if returncode is None or generation == logged_generation:
                continue
            logged_generation = generation
            bridge._record_meta(
                {"event": "dut_exited", "returncode": int(returncode)}
            )
            if not bridge.cfg.allow_dut_exit and not bridge._is_intentional_dut_exit(
                generation
            ):
                bridge._fail("HARNESS_DUT_EXITED", f"returncode={returncode}")
                return
    finally:
        bridge._pump_stopped("dut_watch")


def peer_watch(bridge: Bridge) -> None:
    """每 50 ms 取樣 peer；狀態轉換以 META 保存。"""
    previous: bool | None = None
    try:
        while not bridge._closing and not bridge._stop_event.wait(0.05):
            try:
                current = bridge.endpoint.peer_open()
            except OSError:
                current = None
            if current is None or current == previous:
                continue
            previous = current
            bridge._record_meta({"event": "peer_state", "open": bool(current)})
    finally:
        bridge._pump_stopped("peer_watch")


__all__ = [
    "tx_reader",
    "tx_writer",
    "rx_reader",
    "rx_writer",
    "stderr_drain",
    "dut_watch",
    "peer_watch",
]
