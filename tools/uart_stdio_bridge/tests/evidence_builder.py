"""以測試證據介面建立小型、可重現的 WAL 與 wire 樣本。"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import pathlib
import sys
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
WALL_TS = "2026-10-15T03:06:17+00:00"
CLOCK_ID = "test-monotonic:compare"
OBSERVATIONS = ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_ndjson(path: pathlib.Path, records: Sequence[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(_json_bytes(record) for record in records))


def _read_ndjson(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wal_record(seq: int, direction: str, payload: bytes, *, com: str = "COM0", loss_flag: bool = False) -> dict[str, Any]:
    return {
        "cmd_id": None,
        "com": com,
        "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
        "dir": direction,
        "len": len(payload),
        "loss_flag": loss_flag,
        "meta": {},
        "mono_ts_ns": seq * 1_000_000,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "seq": seq,
        "source": "agent:test",
        "wall_ts": WALL_TS,
    }


def _correlation_record(
    seq: int,
    *,
    op_id: str,
    kind: str,
    action: str,
    wal_seq: int,
    wire_seq: int,
    endpoint_generation: int,
    dut_generation: int,
    offsets: Mapping[str, int],
    settled: bool = True,
    argv: list[str] | None = None,
    cmd_id: str | None = None,
    step_index: int = 0,
) -> dict[str, Any]:
    return {
        "action": action,
        "argv": argv,
        "case_id": "compare-case",
        "clock_id": CLOCK_ID,
        "cmd_id": cmd_id,
        "kind": kind,
        "marker": None,
        "marker_dir": None,
        "mono_ts_ns": seq * 1_000_000,
        "op_id": op_id,
        "rc": None,
        "response_sha256": None,
        "run_id": "compare-run",
        "schema_version": "correlation.v1",
        "seq": seq,
        "settled": settled,
        "step_index": step_index,
        "wal_seq": wal_seq,
        "wall_ts": WALL_TS,
        "wire": {
            "dut_generation": dut_generation,
            "endpoint_generation": endpoint_generation,
            "offsets": dict(offsets),
            "wire_seq": wire_seq,
        },
    }


def _load_evidence_api():
    """延遲載入 Task 2 介面，讓其餘測試可在套件尚未實作時收集。"""
    source = str(SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)
    from uart_stdio_bridge.evidence.manifest import (  # noqa: PLC0415
        bridge_manifest,
        ensure_private_dir,
        write_json_atomic,
    )
    from uart_stdio_bridge.evidence.writer import EvidenceWriter  # noqa: PLC0415

    return EvidenceWriter, bridge_manifest, ensure_private_dir, write_json_atomic


def _load_compare_module(module: str):
    """延遲載入 Task 7 介面，避免尚未實作時造成收集錯誤。"""
    source = str(SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module(f"uart_stdio_bridge.compare.{module}")


def comparator_api():
    module = _load_compare_module("comparator")
    return module.CompareInputs, module.compare, module.check_zero_tx, module.check_contiguous


def rules_api():
    return _load_compare_module("rules").load_rules


def wal_reader_api():
    module = _load_compare_module("wal_reader")
    return module.load_wal, module.list_wal_files


def redact_api():
    return _load_compare_module("redact").redact


@dataclass
class EvidenceSet:
    """由 Task 2 writer 與手寫 WAL 行組成的一組測試證據。"""

    root: pathlib.Path
    evidence_dir: pathlib.Path
    wal_dir: pathlib.Path
    generation_stats: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wire_path(self) -> pathlib.Path:
        return self.evidence_dir / "wire.ndjson"

    @property
    def manifest_path(self) -> pathlib.Path:
        return self.evidence_dir / "bridge-manifest.json"

    @property
    def correlation_path(self) -> pathlib.Path:
        return self.evidence_dir / "correlation.ndjson"

    @property
    def wal_path(self) -> pathlib.Path:
        return self.wal_dir / "raw.wal.ndjson"

    @classmethod
    def clean(cls, root: pathlib.Path) -> "EvidenceSet":
        tx_chunks = (b"alpha\n", b"beta\n", b"gamma\n")
        return cls.build(root, tx_chunks_wire=tx_chunks, tx_chunks_wal=(b"".join(tx_chunks),))

    @classmethod
    def build(
        cls,
        root: pathlib.Path,
        tx_chunks_wire: Sequence[bytes] | None = None,
        tx_chunks_wal: Sequence[bytes] | None = None,
        rx_chunks_wire: Sequence[bytes] | None = None,
        rx_chunks_wal: Sequence[bytes] | None = None,
        settle_points: Sequence[Mapping[str, Any]] | None = None,
        *,
        tx_wire: Sequence[bytes] | None = None,
        tx_wal: Sequence[bytes] | None = None,
        rx_wire: Sequence[bytes] | None = None,
        rx_wal: Sequence[bytes] | None = None,
        op_id: str = "op-1",
        wire_generations: Sequence[Mapping[str, Any]] | None = None,
        closed_cleanly: bool = True,
    ) -> "EvidenceSet":
        """建立 bridge 證據，WAL 行格式依盤點手寫，不呼叫產品程式碼。"""
        EvidenceWriter, bridge_manifest, ensure_private_dir, write_json_atomic = _load_evidence_api()

        if tx_chunks_wire is None:
            tx_chunks_wire = tx_wire or ()
        if tx_chunks_wal is None:
            tx_chunks_wal = tx_wal
        if rx_chunks_wire is None:
            rx_chunks_wire = rx_wire or ()
        if rx_chunks_wal is None:
            rx_chunks_wal = rx_wal

        root = pathlib.Path(root)
        evidence_dir = root / "evidence"
        wal_dir = root / "wal"
        ensure_private_dir(evidence_dir)
        ensure_private_dir(wal_dir)
        ev = cls(root=root, evidence_dir=evidence_dir, wal_dir=wal_dir)

        generations: list[dict[str, Any]]
        if wire_generations is None:
            tx_wire_parts = list(tx_chunks_wire)
            rx_wire_parts = list(rx_chunks_wire)
            tx_wal_parts = list(tx_chunks_wal) if tx_chunks_wal is not None else ([b"".join(tx_wire_parts)] if tx_wire_parts else [])
            rx_wal_parts = list(rx_chunks_wal) if rx_chunks_wal is not None else ([b"".join(rx_wire_parts)] if rx_wire_parts else [])
            generations = [{
                "generation": 1,
                "tx_chunks": tx_wire_parts,
                "rx_chunks": rx_wire_parts,
                "tx_wal_chunks": tx_wal_parts,
                "rx_wal_chunks": rx_wal_parts,
            }]
        else:
            generations = []
            for item in wire_generations:
                tx_parts = list(item.get("tx_chunks", ()))
                rx_parts = list(item.get("rx_chunks", ()))
                tx_wal_parts = list(item.get("tx_wal_chunks", ()))
                rx_wal_parts = list(item.get("rx_wal_chunks", ()))
                if not tx_wal_parts and tx_parts:
                    tx_wal_parts = [b"".join(tx_parts)]
                if not rx_wal_parts and rx_parts:
                    rx_wal_parts = [b"".join(rx_parts)]
                generations.append({
                    "generation": int(item["generation"]),
                    "tx_chunks": tx_parts,
                    "rx_chunks": rx_parts,
                    "tx_wal_chunks": tx_wal_parts,
                    "rx_wal_chunks": rx_wal_parts,
                })
            if not generations:
                generations = [{
                    "generation": 1,
                    "tx_chunks": [],
                    "rx_chunks": [],
                    "tx_wal_chunks": [],
                    "rx_wal_chunks": [],
                }]

        wire_writer = EvidenceWriter(
            ev.wire_path,
            run_id="compare-run",
            case_id="compare-case",
            clock_id=CLOCK_ID,
        )
        wire_writer.start()
        wire_seq = wire_writer.emit_meta(
            port_id="COM0",
            endpoint_generation=generations[0]["generation"],
            dut_generation=1,
            meta={"event": "bridge_start"},
        )
        for generation_index, generation in enumerate(generations):
            number = generation["generation"]
            if generation_index:
                wire_seq = wire_writer.emit_meta(
                    port_id="COM0",
                    endpoint_generation=number,
                    dut_generation=1,
                    meta={
                        "event": "endpoint_generation",
                        "from": generations[generation_index - 1]["generation"],
                        "to": number,
                        "reason": "reconnect",
                    },
                )
            offsets = dict.fromkeys(OBSERVATIONS, 0)
            start_seq = wire_seq
            for payload in generation["tx_chunks"]:
                wire_seq = wire_writer.emit_data(
                    port_id="COM0", endpoint_generation=number, dut_generation=1,
                    observation="serial_read", payload=payload,
                )
                offsets["serial_read"] += len(payload)
                wire_seq = wire_writer.emit_data(
                    port_id="COM0", endpoint_generation=number, dut_generation=1,
                    observation="dut_stdin_write", payload=payload,
                )
                offsets["dut_stdin_write"] += len(payload)
            for payload in generation["rx_chunks"]:
                wire_seq = wire_writer.emit_data(
                    port_id="COM0", endpoint_generation=number, dut_generation=1,
                    observation="dut_stdout_read", payload=payload,
                )
                offsets["dut_stdout_read"] += len(payload)
                wire_seq = wire_writer.emit_data(
                    port_id="COM0", endpoint_generation=number, dut_generation=1,
                    observation="serial_write", payload=payload,
                )
                offsets["serial_write"] += len(payload)
            ev.generation_stats.append({
                "generation": number,
                "start_wire_seq": start_seq,
                "wire_seq": wire_seq,
                "offsets": offsets,
                "tx_wal_chunks": generation["tx_wal_chunks"],
                "rx_wal_chunks": generation["rx_wal_chunks"],
            })

        if closed_cleanly:
            wire_writer.emit_meta(
                port_id="COM0",
                endpoint_generation=generations[-1]["generation"],
                dut_generation=1,
                meta={"event": "bridge_stop"},
            )
        wire_result = wire_writer.close()
        control_path = evidence_dir / "control.ndjson"
        control_path.write_bytes(b"")

        wal_records: list[dict[str, Any]] = []
        for stat in ev.generation_stats:
            for payload in stat["tx_wal_chunks"]:
                wal_records.append(_wal_record(len(wal_records) + 1, "TX", payload))
            for payload in stat["rx_wal_chunks"]:
                wal_records.append(_wal_record(len(wal_records) + 1, "RX", payload))
        _write_ndjson(ev.wal_path, wal_records)

        ev._write_correlation(
            list(settle_points) if settle_points is not None else None,
            op_id=op_id,
            wal_records=wal_records,
            final_wire_seq=wire_result["records"],
        )

        manifest = bridge_manifest(
            run_id="compare-run",
            case_id="compare-case",
            tool={"name": "uart-stdio-bridge", "version": "0.1.0", "tree_sha256": "0" * 64},
            transport={
                "mode": "virtual_pty",
                "port_id": "COM0",
                "daemon_side": "A",
                "bridge_side": "B",
            },
            dut={"mode": "local-process", "name": "test-dut"},
            evidence={
                "wire": {"path": "wire.ndjson", "sha256": wire_result["sha256"]},
                "control": {
                    "path": "control.ndjson",
                    "sha256": hashlib.sha256(control_path.read_bytes()).hexdigest(),
                },
            },
            platform_info={"os": "test", "evidence_insufficient": []},
            started_at=WALL_TS,
            closed_at=WALL_TS if closed_cleanly else None,
            closed_cleanly=closed_cleanly,
            failure=None,
        )
        write_json_atomic(ev.manifest_path, manifest)
        ev._wal_records_cache = wal_records
        return ev

    def _write_correlation(
        self,
        points: Sequence[Mapping[str, Any]] | None,
        *,
        op_id: str,
        wal_records: Sequence[dict[str, Any]],
        final_wire_seq: int,
    ) -> None:
        first_generation = self.generation_stats[0]
        last_generation = self.generation_stats[-1]
        zeros = dict.fromkeys(OBSERVATIONS, 0)
        if points is None:
            points = [
                {
                    "op_id": op_id,
                    "kind": "op_start",
                    "wal_seq": 0,
                    "wire_seq": first_generation["start_wire_seq"],
                    "endpoint_generation": first_generation["generation"],
                    "offsets": zeros,
                    "settled": True,
                },
                {
                    "op_id": op_id,
                    "kind": "op_end",
                    "wal_seq": len(wal_records),
                    "wire_seq": final_wire_seq,
                    "endpoint_generation": last_generation["generation"],
                    "offsets": last_generation["offsets"],
                    "settled": True,
                },
            ]
        records = [self._correlation_from_point(index + 1, point) for index, point in enumerate(points)]
        _write_ndjson(self.correlation_path, records)

    @staticmethod
    def _correlation_from_point(seq: int, point: Mapping[str, Any]) -> dict[str, Any]:
        generation = int(point.get("endpoint_generation", 1))
        return _correlation_record(
            seq,
            op_id=str(point.get("op_id", "op-1")),
            kind=str(point.get("kind", "op_end")),
            action=str(point.get("action", "command.submit")),
            wal_seq=int(point.get("wal_seq", 0)),
            wire_seq=int(point.get("wire_seq", 0)),
            endpoint_generation=generation,
            dut_generation=int(point.get("dut_generation", 1)),
            offsets=point.get("offsets", point.get("wire_offsets", dict.fromkeys(OBSERVATIONS, 0))),
            settled=bool(point.get("settled", True)),
            argv=point.get("argv"),
            cmd_id=point.get("cmd_id"),
            step_index=int(point.get("step_index", 0)),
        )

    def inputs(
        self,
        *,
        rules: Sequence[str] = (),
        secrets: Sequence[bytes] = (),
        until_op: str | None = None,
        checkpoint: pathlib.Path | None = None,
        port_map: Mapping[str, tuple[str, ...]] | None = None,
    ):
        """延遲建立比較器輸入物件。"""
        source = str(SOURCE_ROOT)
        if source not in sys.path:
            sys.path.insert(0, source)
        from uart_stdio_bridge.compare.comparator import CompareInputs  # noqa: PLC0415

        return CompareInputs(
            evidence_dir=self.evidence_dir,
            wal_dir=self.wal_dir,
            port_map=port_map or {"COM0": ("COM0",)},
            rules=tuple(rules),
            secrets=tuple(secrets),
            until_op=until_op,
            checkpoint=checkpoint,
        )

    def apply_tamper(self, tamper: str) -> None:
        """以明確命名的方式破壞一份證據。"""
        wire = _read_ndjson(self.wire_path)
        wal = _read_ndjson(self.wal_path)
        correlation = _read_ndjson(self.correlation_path)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists() else {}
        data = next((record for record in wire if record.get("observation") == "serial_read"), None)
        if tamper == "flip_payload_keep_crc":
            assert data is not None
            payload = base64.b64decode(data["payload_b64"])
            data["payload_b64"] = base64.b64encode(payload[:1] + bytes([payload[1] ^ 1]) + payload[2:]).decode("ascii")
            _write_ndjson(self.wire_path, wire)
        elif tamper == "flip_payload_fix_crc":
            assert data is not None
            payload = base64.b64decode(data["payload_b64"])
            changed = payload[:1] + bytes([payload[1] ^ 1]) + payload[2:]
            data["payload_b64"] = base64.b64encode(changed).decode("ascii")
            data["crc32"] = f"{zlib.crc32(changed) & 0xFFFFFFFF:08x}"
            data["len"] = len(changed)
            _write_ndjson(self.wire_path, wire)
        elif tamper == "delete_middle_record":
            if len(wire) < 4:
                raise AssertionError("竄改樣本至少需要中間 wire 紀錄")
            del wire[len(wire) // 2]
            _write_ndjson(self.wire_path, wire)
            self._refresh_wire_digest(manifest)
        elif tamper == "duplicate_record":
            if not wire:
                raise AssertionError("竄改樣本需要 wire 紀錄")
            wire.insert(2, dict(wire[1]))
            _write_ndjson(self.wire_path, wire)
            self._refresh_wire_digest(manifest)
        elif tamper == "swap_two_records":
            if len(wire) < 3:
                raise AssertionError("竄改樣本至少需要兩筆 wire 紀錄")
            wire[1], wire[2] = wire[2], wire[1]
            _write_ndjson(self.wire_path, wire)
            self._refresh_wire_digest(manifest)
        elif tamper == "change_len":
            assert data is not None
            data["len"] += 1
            _write_ndjson(self.wire_path, wire)
        elif tamper == "set_meta_loss_flag":
            meta = next(record for record in wire if record.get("dir") == "META")
            meta["loss_flag"] = True
            _write_ndjson(self.wire_path, wire)
            self._refresh_wire_digest(manifest)
        elif tamper == "truncate_last_line":
            contents = self.wire_path.read_bytes()
            self.wire_path.write_bytes(contents[:-1])
        elif tamper == "remove_bridge_manifest":
            self.manifest_path.unlink()
        elif tamper == "manifest_closed_uncleanly":
            manifest["closed_cleanly"] = False
            _write_json(self.manifest_path, manifest)
        elif tamper == "wal_flip_payload":
            assert wal
            payload = base64.b64decode(wal[0]["payload_b64"])
            changed = payload[:1] + bytes([payload[1] ^ 1]) + payload[2:]
            wal[0]["payload_b64"] = base64.b64encode(changed).decode("ascii")
            _write_ndjson(self.wal_path, wal)
        elif tamper == "wal_drop_required_key":
            assert wal
            wal[0].pop("source")
            _write_ndjson(self.wal_path, wal)
        elif tamper == "correlation_unsettled":
            assert correlation
            for record in correlation:
                record["settled"] = False
            _write_ndjson(self.correlation_path, correlation)
        else:
            raise ValueError(f"未知竄改：{tamper}")

        if self.manifest_path.exists() and tamper not in {"remove_bridge_manifest", "manifest_closed_uncleanly"}:
            _write_json(self.manifest_path, manifest)

    def _refresh_wire_digest(self, manifest: dict[str, Any]) -> None:
        wire_ref = manifest.get("evidence", {}).get("wire", {})
        wire_ref["sha256"] = hashlib.sha256(self.wire_path.read_bytes()).hexdigest()
        _write_json(self.manifest_path, manifest)

    def set_observation_payload(self, observation: str, payload: bytes, *, refresh_digest: bool = True) -> None:
        wire = _read_ndjson(self.wire_path)
        record = next(item for item in wire if item.get("observation") == observation)
        record["payload_b64"] = base64.b64encode(payload).decode("ascii")
        record["len"] = len(payload)
        record["crc32"] = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
        _write_ndjson(self.wire_path, wire)
        if refresh_digest:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self._refresh_wire_digest(manifest)

    def set_wal_com(self, index: int, com: str) -> None:
        records = _read_ndjson(self.wal_path)
        records[index]["com"] = com
        _write_ndjson(self.wal_path, records)

    def set_wal_loss_flag(self) -> None:
        records = _read_ndjson(self.wal_path)
        if not records:
            records.append(_wal_record(1, "TX", b"x", loss_flag=True))
        else:
            records[0]["loss_flag"] = True
        _write_ndjson(self.wal_path, records)

    def append_wal_reset(self) -> None:
        records = _read_ndjson(self.correlation_path)
        previous = records[-1]
        event = dict(previous)
        event.update({
            "action": "cli",
            "argv": ["wal", "reset"],
            "kind": "marker",
            "marker": "wal reset",
            "op_id": "wal-reset",
            "seq": len(records) + 1,
            "step_index": len(records),
        })
        records.append(event)
        _write_ndjson(self.correlation_path, records)

    def add_reconnect_settle_snapshot(self) -> None:
        records = _read_ndjson(self.correlation_path)
        first = self.generation_stats[0]
        point = {
            "op_id": "transport-reconnect",
            "kind": "op_end",
            "action": "transport_reconnect",
            "wal_seq": len(first["tx_wal_chunks"]) + len(first["rx_wal_chunks"]),
            "wire_seq": first["wire_seq"],
            "endpoint_generation": first["generation"],
            "offsets": first["offsets"],
            "settled": True,
        }
        reconnect = self._correlation_from_point(len(records), point)
        records.insert(max(1, len(records) - 1), reconnect)
        for index, record in enumerate(records, 1):
            record["seq"] = index
        _write_ndjson(self.correlation_path, records)

    def append_correlation_event(self, *, action: str, argv: list[str] | None = None, op_id: str = "extra") -> None:
        records = _read_ndjson(self.correlation_path)
        previous = records[-1]
        event = dict(previous)
        event.update({
            "action": action,
            "argv": argv,
            "kind": "marker",
            "op_id": op_id,
            "seq": len(records) + 1,
            "step_index": len(records),
        })
        records.append(event)
        _write_ndjson(self.correlation_path, records)

    def set_last_correlation_settled(self, value: bool) -> None:
        records = _read_ndjson(self.correlation_path)
        records[-1]["settled"] = value
        _write_ndjson(self.correlation_path, records)

    def replace_correlation(self, records: Sequence[dict[str, Any]]) -> None:
        _write_ndjson(self.correlation_path, list(records))

    def write_checkpoint(self) -> pathlib.Path:
        wire = self.wire_path.read_bytes()
        control = (self.evidence_dir / "control.ndjson").read_bytes()
        checkpoint_dir = self.evidence_dir / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = checkpoint_dir / "compare-checkpoint.json"
        checkpoint = {
            "schema_version": "checkpoint.v1",
            "checkpoint_id": "compare-checkpoint",
            "wire_len": len(wire),
            "wire_sha256": hashlib.sha256(wire).hexdigest(),
            "wire_seq": max((record["seq"] for record in _read_ndjson(self.wire_path)), default=0),
            "control_len": len(control),
            "control_sha256": hashlib.sha256(control).hexdigest(),
        }
        _write_json(path, checkpoint)
        return path

    def install_wal_archive_order_fixture(self) -> None:
        archived = [
            _wal_record(1, "TX", b"archive-a"),
            _wal_record(2, "TX", b"archive-b"),
            _wal_record(3, "TX", b"current"),
        ]
        _write_ndjson(self.wal_dir / "raw.wal.ndjson.20261015-030617", [archived[0]])
        _write_ndjson(self.wal_dir / "raw.wal.ndjson.20261015-030617.1", [archived[1]])
        _write_ndjson(self.wal_path, [archived[2]])

    @classmethod
    def _wal_records_for_test(cls, path: pathlib.Path) -> list[dict[str, Any]]:
        return _read_ndjson(path)


def _write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
