"""RPC metadata 驗證與本體抽取；保留非中繼欄位的原始插入序。"""
from __future__ import annotations

import re

META_KEYS = ("id", "version", "code_fingerprint")


def rpc_body(
    reply: dict,
    *,
    expected_keys: tuple[str, ...],
    required_meta_keys: tuple[str, ...] = (),
) -> dict:
    """驗證 metadata 與完整鍵序，並回傳未改寫的 RPC 本體。"""
    keys = list(reply)
    assert keys == sorted(keys), reply
    assert all(key in reply for key in required_meta_keys), reply
    if "code_fingerprint" in reply:
        fingerprint = reply["code_fingerprint"]
        assert isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{12}", fingerprint), reply
    body = {key: value for key, value in reply.items() if key not in META_KEYS}
    assert tuple(body) == expected_keys, reply
    return body
