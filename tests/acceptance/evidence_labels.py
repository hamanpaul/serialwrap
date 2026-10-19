"""Stage 證據層級標記與防止假冒實機證據的純函式。"""

from __future__ import annotations

import re

LAYERS: frozenset[str] = frozenset({
    "L1.5-local",
    "L1.5-docker",
    "host-throwaway",
    "native-windows",
    "L2-bench",
    "L3-bench",
    "hw-bench",
})
REAL_LAYERS: frozenset[str] = frozenset({"L2-bench", "L3-bench", "hw-bench"})
UART_KINDS: frozenset[str] = frozenset({"native", "pty-fake"})
FAIL_CLOSED_PRODUCERS: frozenset[str] = frozenset({
    "run-l15.sh",
    "run-systemd.sh",
    "run-longrun.sh",
    "run-supervision.sh",
    "win-task-throwaway.ps1",
})


def label_report(
    report: dict,
    *,
    evidence_layer: str,
    producer: str,
    topology: str,
    uart_kind: str,
    image_digests: dict[str, str] | None = None,
    tree_sha256: str | None = None,
) -> dict:
    """回傳附上證據來源標記的新報告，不覆寫衝突的既有層級。"""
    if not isinstance(report, dict):
        raise ValueError("evidence_report_invalid")
    if not isinstance(evidence_layer, str) or evidence_layer not in LAYERS:
        raise ValueError("evidence_layer_unknown")
    if not isinstance(producer, str) or not producer.strip():
        raise ValueError("producer_missing")
    if not isinstance(topology, str) or not topology.strip():
        raise ValueError("topology_missing")
    if not isinstance(uart_kind, str) or uart_kind not in UART_KINDS:
        raise ValueError("uart_kind_unknown")
    if tree_sha256 is not None and (not isinstance(tree_sha256, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", tree_sha256) is None):
        raise ValueError("tree_sha256_invalid")
    if image_digests is not None and (
        not isinstance(image_digests, dict)
        or any(not isinstance(name, str) or not name.strip()
               or not isinstance(digest, str)
               or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
               for name, digest in image_digests.items())
    ):
        raise ValueError("image_digests_invalid")
    if report.get("evidence_layer") not in (None, evidence_layer):
        raise ValueError("evidence_layer_conflict")

    result = dict(report)
    result.update({
        "evidence_layer": evidence_layer,
        "producer": producer,
        "topology": topology,
        "uart_kind": uart_kind,
    })
    if image_digests is not None:
        result["image_digests"] = dict(image_digests)
    if tree_sha256 is not None:
        key = "bundle_tree_sha256" if result.get("implementation") == "rust" else "package_tree_sha256"
        result[key] = tree_sha256
    return result


def validate_report_layer(report: dict) -> str:
    """檢查報告的層級；runner 產出的證據一律缺標即拒收。"""
    if not isinstance(report, dict):
        raise ValueError("evidence_report_invalid")
    layer = report.get("evidence_layer")
    if layer is None:
        raise ValueError("evidence_layer_missing")
    if not isinstance(layer, str) or layer not in LAYERS:
        raise ValueError("evidence_layer_unknown")
    return layer


def layer_satisfies(report_layer: str, required_layer: str) -> bool:
    """判定證據可否滿足索引要求；docker 只可替代本機 L1.5。"""
    if report_layer == required_layer:
        return True
    return required_layer == "L1.5-local" and report_layer == "L1.5-docker"


def assert_not_real_board(report: dict, required_layer: str) -> None:
    """拒絕把 docker 或 throwaway 報告列為真板／實機證據。"""
    layer = report.get("evidence_layer")
    if layer in {"L1.5-docker", "host-throwaway"} and required_layer in REAL_LAYERS:
        raise AssertionError("evidence_layer_mismatch")
    counts_as = report.get("counts_as") or []
    if isinstance(counts_as, str):
        counts_as = [counts_as]
    if not isinstance(counts_as, (list, tuple)) or any(not isinstance(x, str) for x in counts_as):
        raise AssertionError("evidence_layer_invalid_counts_as")
    if REAL_LAYERS.intersection(counts_as):
        raise AssertionError("evidence_layer_mismatch")
