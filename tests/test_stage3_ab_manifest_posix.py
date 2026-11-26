"""Stage 3 T3 的 POSIX symlink 邊界反例。"""

from __future__ import annotations

import os

import pytest

import stage3_fakes as fakes
from tools.stage3 import ab_manifest
from tools.stage3.reason_codes import HarnessError

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]


@pytest.mark.parametrize("link_kind", ["file", "directory"], ids=["file-symlink", "directory-symlink"])
def test_bundle_tree_sha256_rejects_symlink(tmp_path, link_kind):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3")
    if link_kind == "file":
        target = bundle / "bin" / "serialwrap"
        link = bundle / "bin" / "serialwrap-link"
    else:
        target = bundle / "assets" / "profiles"
        link = bundle / "assets" / "profiles-link"
    os.symlink(target, link, target_is_directory=link_kind == "directory")

    for tree_digest in (ab_manifest.bundle_tree_sha256, ab_manifest.runtime_tree_sha256):
        with pytest.raises(HarnessError) as caught:
            tree_digest(bundle)
        assert caught.value.code == "HARNESS_ARTIFACT_MISMATCH"
        assert caught.value.detail == f"non-regular file: {link.relative_to(bundle).as_posix()}"
