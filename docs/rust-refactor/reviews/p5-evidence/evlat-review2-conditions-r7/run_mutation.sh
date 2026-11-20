#!/bin/bash
# 用法（repo 根、一般使用者）：run_mutation.sh <M1|M2|M3|M5|M6|M7> [次數]
# 先備份 crates/sw-core/src/{mailbox,actor}.rs，套用單一變異後連跑 sw-core 的 drain_release_e2e＋mailbox 測試，最後還原。
set -u
M="$1"; N="${2:-3}"; D="$(dirname "$0")"; B="$(mktemp -d)"
cp crates/sw-core/src/mailbox.rs crates/sw-core/src/actor.rs "$B/"
python3 "$D/mutate.py" "$M"
for i in $(seq 1 "$N"); do
  echo "== run $i"
  cargo test -p sw-core --features test-fakes --test drain_release_e2e --test mailbox 2>&1 | grep -E "^error|^test .* FAILED|^test result"
done
cp "$B/mailbox.rs" "$B/actor.rs" crates/sw-core/src/
