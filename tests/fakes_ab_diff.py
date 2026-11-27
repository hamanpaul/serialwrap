"""T7B 報告資料契約與純資料報告樹產生器。

本檔只用標準函式庫及 T2 的 ``tests/stage3_fakes.py``。所有輸出以
``Path.write_bytes`` 寫入固定 UTF-8／LF 位元組；不啟動 adapter、ingest、daemon、
測試產品程序或實機。每次 run 可獨立設定 verdict、安全不變量、CLI 觀測、artifact
身分、完成狀態及缺少的檔案。

報告資料契約（供 T5B adapter 產生真實資料、供 T7B 讀取）：

1. bridge run 沿用 P2 的
   ``<report-dir>/<case_id>/bridge/<run_index>/``。必要檔為
   ``manifest.json``（``schema_version: "l15-manifest.v1"``）、
   ``comparison.json``（``schema_version: "comparison.v1"``）、
   ``bridge-manifest.json``、``wire.ndjson``、``cli.ndjson``、
   ``daemon.log`` 及 ``wal/``。manifest 的 artifact 身分讀
   ``artifacts.cli.sha256``、``artifacts.daemon.implementation``、
   ``artifacts.daemon.sha256``、``artifacts.daemon.package_tree_sha256`` 及
   ``artifacts.release_manifest_sha256``。Python daemon 必須有 package tree
   SHA，``sha256`` 可為 null；Rust daemon 必須有 executable SHA，
   ``package_tree_sha256`` 可為 null。未適用欄位不要求非空。daemon 的
   ``tree_sha256`` 是 T7B 讀取視圖欄名，原始 manifest 位元組不改寫。
   manifest 與 comparison 的 ``case_id``／``run_id`` 必須一致，``case_id``
   必須與目錄名稱一致；目錄 ``run_index`` 是 A/B 配對依據。comparison 的
   verdict 必須是
   ``PASS`` 或 ``FAIL``；``INSUFFICIENT_EVIDENCE`` 不能成為完整證據。
   P2 comparison schema 的 invariant 欄位實際叫 ``verdict``，T7B 以此讀取；
   若其他 runner 原始格式使用 ``result``，adapter 只在讀取視圖中映射，不產生
   或偽造 ``comparison.json``。``cli.ndjson`` 每筆保留 argv、整數 rc、stdout；
   第 i 筆以 rc 及 stdout JSON 進行比較。P2 manifest 必須宣告 ``wire``、
   ``control``、``correlation``、``dut_stderr``、``cli_log``、``wal_dir``；必要
   路徑分別對應實際檔案／目錄，檔案 SHA 必須與實際位元組相符。comparison 的
   ``integrity.wire_sha256`` 與 ``wire_sha256_expected`` 都必須等於實際
   ``wire.ndjson`` SHA。原始檔保持原位元組。

2. 其他 runner 的 run 目錄為
   ``<report-dir>/<case_id>/<runner>/<run_index>/``，其中 runner 僅為
   ``contract-blackbox``、``windows-native``、``rust-l1``。各目錄只新增
   ``runner-observation.json``（讀取視圖，``schema_version:
   "stage3-runner-observation.v1"``）及 ``source/`` 原始檔，不建立
   ``manifest.json``、``comparison.json`` 或 ``bridge-manifest.json``。
   envelope 必含 ``case_id``、``runner``、``run_index``、
   ``completion: {status: "completed", finished: true, exit_code: <int>}``、
   ``artifacts``、``verdict``、``reason``、``invariants``、
   ``cli_observations``、``source_files``、
   ``case_results: [{nodeid, source_ref}]``、``case_map: {path, sha256}``。
   ``case_results.nodeid`` 必須與 ``source/l1-results.json`` 結果中的 nodeid
   完全相等，不得用子字串比對；``source_ref`` 格式為
   ``<case>/<runner>/<run_index>/source/l1-results.json#results[<index>]``，
   定位該原始結果列；該列 nodeid 必須列在原始 ``collected`` 且完全相等。
   ``case_map.path`` 是相對 POSIX 路徑，``sha256`` 是 64 位小寫十六進位字串；
   T7B 僅驗欄位格式並於輸出 ``case_maps`` 保留來源，
   不判斷它是否等於凍結的權威映射。哪個 nodeid 代表哪個 required case，屬
   T5B 與 T6 責任，列為 SPEC-GAP。artifact 身分欄位與 bridge
   manifest 對齊：``artifacts.cli.sha256``、
   ``artifacts.daemon.implementation``、``artifacts.daemon.sha256``、
   ``artifacts.daemon.tree_sha256``、``artifacts.release_manifest_sha256``；
   Python daemon 驗 ``tree_sha256`` 並允許 ``sha256: null``，Rust daemon 驗
   ``sha256`` 並允許 ``tree_sha256: null``。
   依 §0.4 契約 3，A／B 的 ``artifacts.cli.sha256`` 必須相等；不符使用
   ``artifact identity mismatch: artifacts.cli.sha256`` 並使完整性失敗。daemon
   implementation／hash 屬當側 release artifact，兩側不要求相等。
   envelope ``verdict`` 必須和 ``case_results`` 原始結果推得的判定一致：任一
   mapped outcome 為 ``failed`` 即 FAIL，``source_ref`` 指向該列；全部 mapped
   outcome 都是 ``passed`` 才是 PASS。空映射、缺結果、未執行、``skipped``、
   ``error`` 或其他 outcome 都是完整性問題。兩者不一致時以原始結果為準，並記
   完整性問題。每個 invariant 的 ``{id, result}`` 映射到
   comparison.invariants 的 ``{id, verdict}``；安全結果另從原始
   ``source/invariant-evidence.json`` 獨立載入。
   ``cli_observations`` 依原呼叫順序列出 ``{argv, rc, stdout, source_ref}``；
   每筆 rc 必須是整數、stdout 必須是字串，不存在呼叫記錄不得補空字串。每個
   ``source/release-manifest.json``（現行 release manifest、``schema_version: 1``）
   是當側 CLI／daemon artifact 身分的原始來源；envelope 的
   ``artifacts.release_manifest_sha256`` 必須等於該檔原位元組 SHA-256，其他
   ``artifacts.*`` 欄位由其 ``components.cli``／``components.daemon`` 對應。
   ``source_files`` 項目是 ``{path, sha256}``，path 以 run 目錄為根的相對
   POSIX 路徑，sha256 是原始檔位元組雜湊。``source_ref`` 以側 report-dir
   為根的相對 POSIX 路徑，可定位 envelope 內該觀測或 FAIL 的原始來源。

   - contract-blackbox：原始 ``source/l1-results.json`` 使用現行 P2
     ``schema_version: 2``、``format: "l1-results.v1"``（pytest runner）；
     另列原始 ``source/junit.xml`` 及逐呼叫擷取的
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``（``stage3-invariant-evidence.v1``）。adapter
     verdict／invariants 是讀取視圖，不改寫上述原報告。
   - windows-native：原始 ``source/l1-results.json`` 同為
     ``l1-results.v1``（pytest runner、platform=windows），另必列
     ``source/windows-transport-context.json``（T5B 的
     ``stage3-windows-transport.v1``，含實際 platform、transport、serial
     backend、port_id、是否實際開埠及 context SHA）及
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``。原生 COM／OD-08 實體替代的實際開埠
     證據不得由預定 manifest 值回填；依 R12-01 的去識別與非公開來源規則處理。
     兩種 pytest runner 的 L1 都由
     ``tests/resource_report_plugin.py::pytest_sessionfinish`` 產生；Windows 原始
     報告附 ``pytest-l1-provenance.v1``，Linux 原始報告不含 pytest provenance。
   - rust-l1：原始 ``source/l1-results.json`` 使用現行
     ``l1-results.v1``（schema_version=2）；頂層 ``runner`` 與 ``finished``
     是 Cargo 收集器輸出的 runner／完成狀態。``provenance`` 僅驗
     ``scripts/rust/cargo_l1_results.py`` 的 ``_provenance()`` 實際輸出欄位，
     不要求原始報告沒有的 ``provenance.runner`` 或 ``provenance.complete``。
     另列
     ``source/libtest.stdout.txt`` 及
     ``source/cli-observations.ndjson``、
     ``source/invariant-evidence.json``。cargo／pytest 本身的通過只證明測試
     runner 完成；只有可對應 required case 的受測產品觀測才能成為 verdict、
     safety 或 CLI 差分來源。

   身分只有在原始 ``source/release-manifest.json`` 存在且可解析、該檔列於
   ``source_files`` 且位元組 SHA 已核實、適用 artifact 欄位與 envelope 一致時
   才算已核實；envelope 中格式正確的 SHA 不足以單獨證明身分。
   ``source/cli-observations.ndjson`` 使用 T5B 約定的
   ``stage3-cli-observations.v1`` 記錄，逐行保存呼叫序號、argv、整數 rc 與
   原始 stdout。``source_files`` 必列出該 runner 產生的全部必要來源檔與 SHA；
   loader 必核對檔案存在、SHA 相符、觀測 source_ref 可定位。讀取視圖不修改
   原始報告，不以檔名不同為由略過 verdict／安全結果，不把非 bridge 輸出偽裝
   成 P2 bridge 檔。

3. 缺漏採 fail-closed，缺漏類型不互相代替：缺 required case 為
   ``missing required case: <case_id>``；``case_changes`` 引用的變更 ID 不在 ``registered`` 時，兩側都記
   ``unregistered case change: <case_id> -> <change_id>``；缺 source_files 所列原檔或 SHA 不符為
   ``missing source file: <relative_path>``／``source hash mismatch:
   <relative_path>``；缺 CLI 記錄、rc 或 stdout 為
   ``missing required observation: cli_observations[<i>].<field>``（例如 argv、rc、stdout）；缺 CLI／daemon
   身分欄位或必要身分值為 ``missing artifact identity: <dotted_field>``；
   envelope 或原始 runner 未完成、envelope／原始退出碼非零或不一致、必要 case
   結果非 passed/failed、缺結果或未執行，都記入 ``integrity_problems`` 並令該側
   ``integrity=false``；envelope 缺漏時仍檢查原始 L1 報告的完成狀態與退出碼。
   退出碼不能單獨推導產品 FAIL。輸出新增
   ``baseline_failures``（A 側一般 FAIL）、``unverified_failures``（來源／身分／
   案例未核實的 FAIL 線索，另含 ``problems``）、``product_failures``（只含 B 側
   身分與來源已核實且對應 required case 的一般 FAIL）。安全 invariant FAIL 兩側
   都保留在 ``safety_violations``；來源未核實者另記於 ``unverified_failures`` 並
   令該側 integrity=false。只有對應 ``required_case_ids`` 的 B 側一般 FAIL 才列入
   ``product_failures``；A 側一般 FAIL 列入 ``baseline_failures``。任何可解析但尚未
   核實的 FAIL 線索都保留在 ``unverified_failures``，且該側必須有對應的
   ``integrity_problems``、``integrity=false`` 與 ``ok=false``。資料欄位型別錯誤轉成該 run
   的完整性診斷並繼續掃描；只有呼叫端參數無效才回報 HarnessError。bridge 缺
   ``manifest.json``、``comparison.json``、``bridge-manifest.json``、
   ``wire.ndjson``、``cli.ndjson``、``daemon.log`` 或 ``wal/``，逐項使用
   ``missing source file: <relative_path>``；P2 manifest 宣告的任一檔案 SHA 不符
   使用 ``source hash mismatch: <relative_path>``。bridge comparison verdict 為
   ``INSUFFICIENT_EVIDENCE`` 時使用
   ``invalid comparison verdict: INSUFFICIENT_EVIDENCE``；
   ``bridge-manifest.json.closed_cleanly`` 非 true 使用
   ``bridge not closed cleanly: bridge-manifest.json.closed_cleanly``；
   ``bridge-manifest.json.failure`` 非 null 使用
   ``bridge failure: bridge-manifest.json.failure``。不能以空 observation 或合成空檔
   當成兩側相同。

   回傳另含 ``artifact_identity: [{side, case, runner, run_index, verified,
   source_ref}]``，逐 run 記錄當側 artifact／原始身分來源核實狀態；以及
   ``cli_pairing: [{case, runner, run_index, matched, source_refs}]``，逐 A/B
   run 記錄 CLI SHA 是否相同（SHA 缺漏或格式錯誤時 ``matched`` 為 null）。
   pairing 不改寫任一側的 ``artifact_identity.verified``；CLI SHA 不同仍會令
   B 側 integrity 失敗，但不降級當側已核實的產品或安全 FAIL。

4. ``scope: "rust_only"`` 且 ``ab_participation: "absolute_only"`` 的 BC
   仍由 T7B 保留差異與 ``registered_change``；T7 依 ``absolute_only`` 處理，
   B 側 FAIL 仍須保留。現行依據是
   ``contracts/behavior_changes.yaml`` 的 ``changes[]`` 欄位
   ``scope``、``ab_participation``、``case_ids``；例如 BC-06 為 rust_only /
   absolute_only，且列出 ``bc06-shutdown-self-test-flush``。

5. CLI 呼叫以序號逐筆配對並比較 logical argv。移除 argv[0] 的 CLI 執行檔
   （Python parser 的 ``prog="serialwrap"`` 定義於 ``sw_core/cli.py:2148-2150``；
   Rust root spec 定義於 ``crates/sw-cli/src/cli_spec.rs:1893``），再只在 root
   parser 的子命令之前移除真正的實例路由選項：``--socket``（
   ``sw_core/cli.py:2162``、``crates/sw-cli/src/cli_spec.rs:1904``）、
   ``--endpoint``（``sw_core/cli.py:2163``、
   ``crates/sw-cli/src/cli_spec.rs:1905``；解析優先序見
   ``sw_core/cli.py:876-900`` 與 ``crates/sw-cli/src/endpoint_resolve.rs:230-300``）、
   ``--bench``（``sw_core/cli.py:2164-2168``、
   ``crates/sw-cli/src/cli_spec.rs:1906``；bench endpoint 解析見
   ``sw_core/cli.py:876-900`` 與 ``crates/sw-cli/src/endpoint_resolve.rs:230-300``）及
   各自緊接的一個值。三者是實例路由選項：socket／endpoint 選定 daemon endpoint，
   bench 依已記住的 bench 解析 endpoint。支援 ``--flag=value``；分離值只移除
   選項後一個 token。遇到 ``--`` 後保留所有 token；一旦進入子命令，該層位置參數
   與選項一律保留，即使 token 名稱與 root 選項相同。其他 root 選項也保留，包含
   ``--timeout``（``sw_core/cli.py:2170-2175``、
   ``crates/sw-cli/src/cli_spec.rs:1907``）、``--retries``
   （``sw_core/cli.py:2177-2183``、``crates/sw-cli/src/cli_spec.rs:1908``）、
   ``-v``／``--verbose``（``sw_core/cli.py:2161``、
   ``crates/sw-cli/src/cli_spec.rs:1903``）、``--version``
   （``sw_core/cli.py:2160``、``crates/sw-cli/src/cli_spec.rs:1902``）、
   ``-h``／``--help``（Python ``ArgumentParser`` 的 ``add_help=True`` 見
   ``sw_core/cli.py:2149``；Rust root spec 見
   ``crates/sw-cli/src/cli_spec.rs:1899-1901``）；它們不是實例路由參數。
   子命令的 ``--cmd-id``
   （``sw_core/cli.py:2395-2401``）同樣保留。Rust root spec 對應位置在
   ``crates/sw-cli/src/cli_spec.rs:1899-1908``；此處沒有 Rust CLI 實際 argv 執行。
   bridge ``cli.ndjson`` 與非 bridge ``cli_observations`` 的 argv 都必須是非空字串
   清單，移除 CLI 路徑與實例路由選項後仍須有 root 子命令位置參數；否則分別記
   ``missing required observation: cli_observations[<i>].argv (cli.ndjson#L<n>)``
   與 ``missing required observation: cli_observations[<i>].argv``。
   剩餘每個參數值套用該側 instance root 替換為 ``<ROOT>``。命令不同時新增一筆
   ``{kind: "argv", path:
   "call[<i>].argv", source_refs: {a, b}}`` 差異；呼叫數不同時，每一筆多出或
   缺少的呼叫各新增 ``{kind: "call_count", path: "call[<i>]",
   source_refs: {a, b}}``，缺少側的 source_ref 為 null。這兩個 kind 是 plan
   介面新增值，T7 依未登錄差異判 FAIL。

6. 產品／安全 FAIL 先收集，再彙整 integrity；不得因缺案或 runner 未完成提前返回。
   ``baseline_failures`` 與 ``product_failures`` 每筆為
   ``{side, case, source_ref, reason}``；``unverified_failures`` 每筆另含
   ``problems``。原始 invariant 項目以 JSON Pointer index（例如
   ``comparison.json#/invariants/3``）定位；識別欄位無效時仍留存 FAIL 與原因。
   來源引用相對該側 report-dir 且必須指向原始可解析 FAIL 來源。
   安全 invariant FAIL 另外保留為 ``<A|B>:<case>:<invariant_id>``；安全 FAIL 不受 B11 欄位正規化
   影響。每筆差異必含 ``source_refs: {a, b}``，分別為兩側觀測的相對來源引用，
   且可定位原始觀測檔／line；不以 report 根絕對路徑作 provenance。
   ``unverified_failures`` 非空的一側，必須在該側
   ``integrity_problems`` 有對應診斷、``integrity`` 必為 false，並因此令
   ``ok`` 為 false。

7. B11 暫定觀測範圍：候選規則只允許忽略邏輯命令為 ``daemon status`` 或
   ``health.status``、CLI ``rc == 0`` 且 stdout JSON ``ok is true`` 的回應中最上層
   ``foreign_holders`` 掃描快照。空 map 可忽略；非空 map 必須每一項的鍵都是以
   ``/dev/`` 開頭的絕對裝置路徑字串、值是非 bool 整數，且等於回應頂層 ``pid``，
   才可忽略。依據是 ``sw_core/multi_open.py`` 的 ``detect_multi_open(tty_paths=...)``
   以呼叫端傳入的 tty 路徑作為 ``holders`` 鍵，``sw_core/service.py`` 從 session 的
   ``attached_real_path`` 建立 ``tty_paths`` 並將 ``mo["holders"]`` 輸出為頂層
   ``foreign_holders``；本地程序／PTY DUT 的路徑形如 ``/dev/pts/N``。因此其他鍵，
   包含 ``holder_unknown``、``device``、相對路徑、Windows 裝置形式及任何非
   ``/dev/`` 開頭字串，都必須保留整個欄位並嚴格比較。

   Windows 路徑的靜態查證結果：不能說 Windows daemon 的 ``multi_open`` 完全不嘗試
   掃 ``/proc``。``service.py`` 仍會呼叫 ``detect_multi_open``，而該函式預設
   ``proc_root="/proc"`` 並嘗試列舉它；若 procfs 不可用，``OSError`` 分支回傳空
   ``holders`` 與 ``holders_status="unknown"``。之後 service 仍可把
   ``unknown_holder_devices()`` 的項目合併進 map；Windows 裝置來源使用
   ``\\\\.\\COMx`` 路徑，這些鍵不符合 ``/dev/`` 條件。因此在目前 Windows 原生
   daemon 輸出路徑中，這條候選規則不會忽略任何非空 map。此結論來自上述程式路徑，
   未以 Windows 原生執行驗證。

   通用 volatile key 刪除與 instance-root 替換不進入 ``foreign_holders`` 子樹。
   命令不符、非成功回應、巢狀同名欄位、危險 holder、``holder_unknown`` 及任何安全
   invariant 都必須保留；外來 PID 或 unknown 欄位即使位於成功 status 回應最上層
   也須保留差異。將整個欄位無條件刪除會掩蓋這些差異。此項未經獨立審查或擁有者
   追認，不得凍結為 approved normalization。

8. ``differences[]`` 每列含 ``verified: bool``，只依該差異實際引用的來源與身分判定。
   ``value``、``rc``、``argv``、``call_count`` 引用 CLI 原始觀測，須核實兩側 run／case
   身分，以及存在側的 CLI 原始檔 hash；同 case 其他未核實線索不會單獨降級差異。
   ``call_count`` 缺少側的 ``source_refs`` 為 null，只核對存在側來源，但仍須核對兩側
   run／case 身分。``verdict`` 的 bridge 來源為 ``comparison.json``，依 manifest 與
   comparison 的 case／run 身分及 artifact 身分核實；不要求 comparison 出現在 manifest
   evidence hash 清單中，但若有該檔的 hash 宣告則必須核對。非 bridge verdict 引用
   ``source/l1-results.json`` 的對應結果列，且只在該列已映射、原始檔 hash、run 與 case
   身分均核實時為 true。

這份契約只規範 T7B loader／fixture 與後續 T5B adapter 的讀取介面。它不代表目前
repo 已有非 bridge adapter，也不宣稱純資料 fixture 是產品執行證據。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from stage3_fakes import sha256_file, wal_record


RUNNERS = ("bridge", "contract-blackbox", "windows-native", "rust-l1")
OBSERVATION_SCHEMA = "stage3-runner-observation.v1"
CLI_OBSERVATIONS_SCHEMA = "stage3-cli-observations.v1"
CASE_ID = "l15-fixture-case"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_json(path: Path, value: Any) -> Path:
    return _write_bytes(path, _json_bytes(value))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_identity(side: str) -> dict[str, Any]:
    is_a = side.lower() == "a"
    daemon: dict[str, Any] = {
        "implementation": "python" if is_a else "rust",
        "sha256": None if is_a else "3" * 64,
        "tree_sha256": "4" * 64 if is_a else None,
    }
    return {
        "cli": {"sha256": "1" * 64},
        "daemon": daemon,
        "release_manifest_sha256": "6" * 64,
    }


def _merge_mapping(target: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for key, value in overrides.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_mapping(current, value)
        else:
            target[key] = value


def _deployed_content(identity: Mapping[str, Any]) -> dict[str, Any]:
    cli_sha = identity.get("cli", {}).get("sha256")
    daemon_sha = identity.get("daemon", {}).get("sha256")
    return {
        "schema_version": "deployed-content.v1",
        "measured_at": "2026-11-27T00:00:00+00:00",
        "cli_configured": "fixture-cli",
        "cli_resolved": "fixture-cli",
        "cli_sha256": cli_sha,
        "install_dir": "fixture-install",
        "release_manifest_sha256": identity.get("release_manifest_sha256"),
        "bundle_tree_sha256": "a" * 64,
        "runtime_tree_sha256": "b" * 64,
        "entry_dir_sha256": "c" * 64,
        "daemon_configured": ["fixture-daemon"],
        "daemon_resolved": "fixture-daemon",
        "daemon_sha256": daemon_sha,
        "error": None,
    }


def _release_manifest_bytes(identity: Mapping[str, Any]) -> bytes:
    cli = identity.get("cli", {})
    daemon = identity.get("daemon", {})
    return _json_bytes({
        "schema_version": 1,
        "bundle_version": "0.5.1",
        "stage": "3",
        "created_at": "2026-11-27T00:00:00+00:00",
        "target": "fixture-target",
        "git_revision": "0" * 40,
        "contract_revision": "1" * 40,
        "components": {
            "cli": {
                "implementation": "rust",
                "version": "0.5.1",
                "git_revision": "0" * 40,
                "path": "bin/serialwrap",
                "sha256": cli.get("sha256"),
            },
            "daemon": {
                "implementation": daemon.get("implementation"),
                "version": "0.5.1",
                "git_revision": "0" * 40,
                "launcher": {"path": "bin/serialwrapd", "sha256": daemon.get("sha256")},
                "entry": ["bin/serialwrapd"],
                "runtime": {"path": "runtime", "python_version": "3.12", "sha256": "8" * 64},
                "package": {
                    "name": "serialwrap",
                    "version": "0.5.1",
                    "wheel_sha256": None,
                    "tree_sha256": daemon.get("tree_sha256"),
                },
            },
            "python_cli_delegate": {"entry": ["fixture"], "tree_sha256": "9" * 64},
            "assets": {"skill_sha256": "a" * 64, "skill_windows_sha256": "b" * 64, "profiles_sha256": "c" * 64},
            "supervisor": {"default_mode": "on-demand", "definition_sha256": "d" * 64},
        },
        "pairing": {"rule": "exact", "cli_version": "0.5.1", "daemon_version": "0.5.1"},
    })


def _default_call(side: str) -> dict[str, Any]:
    return {
        "argv": ["daemon", "status"],
        "rc": 0,
        "response": {"ok": True, "pid": 4101 if side.lower() == "a" else 4202, "state": "READY"},
    }


def _as_call(call: Mapping[str, Any], side: str) -> dict[str, Any]:
    record: dict[str, Any] = {"argv": list(call.get("argv", ())) }
    if "rc" in call:
        record["rc"] = call["rc"]
    if "stdout" in call:
        record["stdout"] = call["stdout"]
    elif "response" in call:
        record["stdout"] = json.dumps(call["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record.setdefault("stderr", "")
    if "mono_start_ns" in call:
        record["mono_start_ns"] = call["mono_start_ns"]
    return record


def _source_specs(
    runner: str,
    *,
    complete: bool,
    verdict: str,
    invariants: Iterable[Mapping[str, Any]],
    source_invariants: Iterable[Mapping[str, Any]] | None,
    release_manifest: bytes,
    runner_exit_code: int | None,
) -> dict[str, bytes]:
    outcome = "failed" if verdict == "FAIL" else "passed"
    if not complete:
        outcome = "error"
    l1_report = {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "cargo" if runner == "rust-l1" else "pytest",
        "platform": "windows" if runner == "windows-native" else "linux",
        "argv": ["fixture-runner"],
        "markexpr": "",
        "counts": {"all": {"passed": int(outcome == "passed"), "failed": int(outcome == "failed"), "skipped": 0}},
        "resources": {},
        "uart_kind": "native" if runner == "windows-native" else "pty-fake",
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": ["fixture-nodeid"],
        "deselected": [],
        "results": [{
            "nodeid": "fixture-nodeid",
            "outcome": outcome,
            "skip_kind": None,
            "phases": {"setup": "passed", "call": "failed" if outcome == "failed" else "passed", "teardown": "passed"},
        }],
        "exit_code": runner_exit_code if runner_exit_code is not None else (1 if outcome in {"failed", "error"} else 0),
        "finished": bool(complete),
        "deployed_content": None,
    }
    if runner == "windows-native":
        # 對應 tests.resource_report_plugin.py::pytest_sessionfinish 的原生 Windows provenance。
        l1_report["provenance"] = {
            "schema": "pytest-l1-provenance.v1",
            "platform": "windows",
            "git": {
                "head": "0" * 40,
                "clean": True,
                "end": {"head": "0" * 40, "clean": True},
            },
        }
    if runner == "rust-l1":
        # 對應 scripts/rust/cargo_l1_results.py::_provenance()；完成狀態與 runner
        # 位於 l1_report 頂層，不是 provenance 欄位。
        l1_report["provenance"] = {
            "schema": "cargo-l1-provenance.v1",
            "platform": "linux",
            "build_target": None,
            "git": {
                "head": "0" * 40,
                "clean": True,
                "end": {"head": "0" * 40, "clean": True},
            },
            "toolchain": {
                "rustc_vv": "rustc fixture\n",
                "rustc_vv_sha256": _sha256(b"rustc fixture\n"),
                "error": None,
            },
            "rustflags": {
                "RUSTFLAGS": {"set": False, "sha256": None},
                "CARGO_ENCODED_RUSTFLAGS": {"set": False, "sha256": None},
            },
            "executables": [{
                "crate": "serialwrap",
                "target": "lib",
                "path": "fixture-cargo-test",
                "sha256": "d" * 64,
                "nodeids": ["fixture-nodeid"],
            }],
        }
    specs: dict[str, bytes] = {
        "source/release-manifest.json": release_manifest,
        "source/l1-results.json": _json_bytes(l1_report),
        "source/invariant-evidence.json": _json_bytes({
            "schema_version": "stage3-invariant-evidence.v1",
            "invariants": [dict(item) for item in (invariants if source_invariants is None else source_invariants)],
        }),
    }
    if runner in {"contract-blackbox", "windows-native"}:
        failure_xml = '<failure message="fixture failure"/>' if verdict == "FAIL" else ""
        specs["source/junit.xml"] = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<testsuites><testsuite name=\"stage3\" tests=\"1\" "
            f"failures=\"{int(verdict == 'FAIL')}\">"
            f"<testcase name=\"fixture-node\">{failure_xml}</testcase></testsuite></testsuites>\n"
        ).encode("utf-8")
    if runner == "windows-native":
        specs["source/windows-transport-context.json"] = _json_bytes({
            "schema_version": "stage3-windows-transport.v1",
            "platform": "windows",
            "transport": "windows-native-com",
            "serial_backend": "native-com",
            "port_id": "fixture-port",
            "opened": True,
            "candidate_context_sha256": "7" * 64,
        })
    if runner == "rust-l1":
        specs["source/libtest.stdout.txt"] = (
            b"test fixture_node ... FAILED\n" if verdict == "FAIL" else b"test fixture_node ... ok\n"
        )
    return specs


def _source_rows(run_dir: Path, specs: Mapping[str, bytes]) -> list[dict[str, str]]:
    return [
        {"path": rel, "sha256": _sha256(data)}
        for rel, data in sorted(specs.items())
    ]


def write_run(
    report_dir: Path,
    case_id: str,
    *,
    side: str,
    runner: str = "bridge",
    run_index: int = 1,
    verdict: str = "PASS",
    reason: str = "fixture result",
    invariants: Iterable[Mapping[str, Any] | tuple[str, str] | str] = (),
    calls: Iterable[Mapping[str, Any]] | None = None,
    complete: bool = True,
    runner_exit_code: int | None = None,
    missing: Iterable[str] = (),
    identity_missing: Iterable[str] = (),
    artifact_overrides: Mapping[str, Any] | None = None,
    observations: Iterable[Mapping[str, Any]] | None = None,
    l1_results: Iterable[Mapping[str, Any]] | None = None,
    case_results: Iterable[Mapping[str, str]] | None = None,
    case_map: Mapping[str, Any] | None = None,
    source_invariants: Iterable[Mapping[str, Any]] | None = None,
) -> Path:
    """在 report_dir 建立一筆 run；missing 使用 run 內相對路徑。"""
    if runner not in RUNNERS:
        raise ValueError(f"不支援的 fixture runner：{runner}")
    if verdict not in {"PASS", "FAIL", "INSUFFICIENT_EVIDENCE"}:
        raise ValueError(f"不支援的 fixture verdict：{verdict}")
    root = Path(report_dir)
    run_dir = root / case_id / runner / str(run_index)
    missing_set = set(missing)
    identity = _artifact_identity(side)
    if artifact_overrides:
        _merge_mapping(identity, artifact_overrides)
    for dotted in identity_missing:
        keys = dotted.split(".")
        node: dict[str, Any] = identity
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                break
            node = child
        else:
            node.pop(keys[-1], None)

    normalized_invariants: list[dict[str, Any]] = []
    for invariant in invariants:
        if isinstance(invariant, str):
            normalized_invariants.append({"id": invariant, "result": "PASS"})
        elif isinstance(invariant, tuple):
            normalized_invariants.append({"id": invariant[0], "result": invariant[1]})
        else:
            normalized_invariants.append(dict(invariant))
    if not normalized_invariants:
        normalized_invariants = [{"id": "zero_tx_on_reject", "result": "PASS"}]
    if verdict == "FAIL" and reason == "fixture result":
        reason = "fixture case failed"

    default_calls = [_default_call(side)] if calls is None else list(calls)
    call_rows = [_as_call(call, side) for call in default_calls]
    if observations is not None:
        call_rows = [dict(row) for row in observations]

    if runner == "bridge":
        run_dir.mkdir(parents=True, exist_ok=True)
        bridge_calls: list[dict[str, Any]] = []
        for row in call_rows:
            if "argv" in row:
                argv = list(row["argv"])
                full_argv = ["serialwrap", "--endpoint", f"{side.upper()}_INSTANCE_ROOT/run/serialwrapd.sock", *argv]
                bridge_calls.append({**row, "argv": full_argv})
            else:
                bridge_calls.append(dict(row))
        cli_bytes = b"".join(_json_bytes(row) for row in bridge_calls)
        wire_bytes = _json_bytes({"dir": "TX", "dut_generation": 1, "bytes": 0})
        control_bytes = b"{}\n"
        correlation_bytes = b"{}\n"
        dut_stderr_bytes = b"fixture DUT output\n"
        wal_bytes = _json_bytes(wal_record(1, dir="TX", payload=b""))
        comparison = {
            "schema_version": "comparison.v1",
            "run_id": f"{case_id}-{run_index}",
            "case_id": case_id,
            "verdict": verdict,
            "reasons": [reason] if verdict == "FAIL" else [],
            "tool": {"name": "fixture", "version": "1", "tree_sha256": "6" * 64},
            "integrity": {"wire_records": 1, "wire_sha256": _sha256(wire_bytes),
                          "wire_sha256_expected": _sha256(wire_bytes), "wal_files": ["wal/raw.wal.ndjson"],
                          "wal_records": 1, "problems": []},
            "window": {"until_op": None, "wal_seq": 1, "wire_offsets": {"fixture-port": 0}, "endpoint_generation": 1},
            "streams": [],
            "invariants": [
                {"id": item["id"], "verdict": item["result"], "detail": str(item.get("detail", "fixture invariant"))}
                for item in normalized_invariants
            ],
        }
        files: dict[str, bytes] = {
            "wire.ndjson": wire_bytes,
            "control.ndjson": control_bytes,
            "correlation.ndjson": correlation_bytes,
            "dut.stderr.log": dut_stderr_bytes,
            "cli.ndjson": cli_bytes,
            "daemon.log": b"fixture daemon output\n",
            "comparison.json": _json_bytes(comparison),
            "bridge-manifest.json": _json_bytes({"closed_cleanly": bool(complete), "failure": None if complete else "fixture incomplete"}),
            "wal/raw.wal.ndjson": wal_bytes,
        }
        file_hashes = {name: _sha256(data) for name, data in files.items()}
        manifest = {
            "schema_version": "l15-manifest.v1",
            "run_id": f"{case_id}-{run_index}",
            "case_id": case_id,
            "created_at": "2026-11-27T00:00:00+00:00",
            "mode": "bridge",
            "topology": "dut_local_process_daemon_native",
            "transport": {"mode": "virtual_pty", "port_id": "fixture-port", "daemon_side": None, "bridge_side": None},
            "artifacts": {
                "cli": {"path": "fixture-cli", "sha256": identity.get("cli", {}).get("sha256"),
                        "version_stdout": "serialwrap fixture"},
                "daemon": {
                    "argv": ["fixture-daemon"],
                    "implementation": identity.get("daemon", {}).get("implementation"),
                    "sha256": identity.get("daemon", {}).get("sha256"),
                    "package_tree_sha256": identity.get("daemon", {}).get("tree_sha256"),
                    "repo_tree_sha256": "9" * 64 if side.lower() == "a" else None,
                },
                "release_manifest_sha256": identity.get("release_manifest_sha256"),
                "deployed_content": {
                    "start": _deployed_content(identity),
                    "end": _deployed_content(identity),
                    "daemon_live": [],
                },
            },
            "tool": {"name": "fixture", "version": "1", "tree_sha256": "6" * 64, "approved": True},
            "dut": {"mode": "local-process", "image": None, "image_id": None,
                    "target_sha256": "7" * 64, "scenario_sha256": None, "seed": None},
            "repo_git_revision": None,
            "repo_dirty": False,
            "contract_revision": None,
            "contract_dirty": False,
            "cases_yaml_sha256": None,
            "case": {"source_path": "fixture", "sha256": "8" * 64, "revision": None},
            "platform": {"os": "fixture", "release": "fixture", "version": "fixture", "machine": "fixture",
                         "python": "3", "evidence_insufficient": []},
            "bridge_manifest_sha256": file_hashes["bridge-manifest.json"],
            "evidence": {
                "wire": {"path": "wire.ndjson", "sha256": file_hashes["wire.ndjson"]},
                "control": {"path": "control.ndjson", "sha256": file_hashes["control.ndjson"]},
                "correlation": {"path": "correlation.ndjson", "sha256": file_hashes["correlation.ndjson"]},
                "dut_stderr": {"path": "dut.stderr.log", "sha256": file_hashes["dut.stderr.log"]},
                "cli_log": {"path": "cli.ndjson", "sha256": file_hashes["cli.ndjson"]},
                "wal_dir": "wal",
            },
        }
        files["manifest.json"] = _json_bytes(manifest)
        for rel, data in files.items():
            if rel not in missing_set and not any(rel.startswith(m.rstrip("/") + "/") for m in missing_set):
                _write_bytes(run_dir / rel, data)
        return run_dir

    run_dir.mkdir(parents=True, exist_ok=True)
    release_manifest = _release_manifest_bytes(identity)
    if "release_manifest_sha256" in identity:
        identity["release_manifest_sha256"] = _sha256(release_manifest)
    source_specs = _source_specs(
        runner,
        complete=complete,
        verdict=verdict,
        invariants=normalized_invariants,
        source_invariants=source_invariants,
        release_manifest=release_manifest,
        runner_exit_code=runner_exit_code,
    )
    case_nodeids: list[str] = []
    if runner in {"contract-blackbox", "windows-native"}:
        l1 = json.loads(source_specs["source/l1-results.json"].decode("utf-8"))
        nodeid = "tests/contract_blackbox/test_l15_observation.py::test_required_observation"
        l1["collected"] = [nodeid]
        l1["results"][0]["nodeid"] = nodeid
        case_nodeids = [nodeid]
        source_specs["source/l1-results.json"] = _json_bytes(l1)
    else:
        l1 = json.loads(source_specs["source/l1-results.json"].decode("utf-8"))
        nodeid = "serialwrap::tests::stage3::required_observation"
        l1["collected"] = [nodeid]
        l1["results"][0]["nodeid"] = nodeid
        case_nodeids = [nodeid]
        if runner == "rust-l1":
            l1["provenance"]["executables"][0]["nodeids"] = [nodeid]
        source_specs["source/l1-results.json"] = _json_bytes(l1)
    if l1_results is not None:
        l1 = json.loads(source_specs["source/l1-results.json"].decode("utf-8"))
        l1["results"] = [dict(row) for row in l1_results]
        case_nodeids = [row["nodeid"] for row in l1["results"] if isinstance(row.get("nodeid"), str)]
        l1["collected"] = list(case_nodeids)
        source_specs["source/l1-results.json"] = _json_bytes(l1)
    raw_call_rows = [
        {
            "call_index": index,
            **({"argv": row["argv"]} if "argv" in row else {}),
            **({"rc": row["rc"]} if "rc" in row else {}),
            **({"stdout": row["stdout"]} if "stdout" in row else {}),
        }
        for index, row in enumerate(call_rows)
    ]
    source_specs["source/cli-observations.ndjson"] = b"".join(_json_bytes(row) for row in raw_call_rows)
    for rel, data in source_specs.items():
        if rel not in missing_set:
            _write_bytes(run_dir / rel, data)

    source_rows = _source_rows(run_dir, source_specs)
    if case_results is None:
        normalized_case_results = [
            {
                "nodeid": nodeid,
                "source_ref": f"{case_id}/{runner}/{run_index}/source/l1-results.json#results[{index}]",
            }
            for index, nodeid in enumerate(case_nodeids)
        ]
    else:
        normalized_case_results = [dict(row) for row in case_results]
    case_map_value = {"path": "tests/acceptance/cases.yaml", "sha256": "e" * 64}
    if case_map is not None:
        case_map_value = dict(case_map)
    envelope_observations = [
        {
            "argv": list(row.get("argv", ())),
            **({"rc": row["rc"]} if "rc" in row else {}),
            **({"stdout": row["stdout"]} if "stdout" in row else {}),
            "source_ref": f"{case_id}/{runner}/{run_index}/source/cli-observations.ndjson#L{index + 1}",
        }
        for index, row in enumerate(call_rows)
    ] if observations is None else [dict(row) for row in observations]
    envelope = {
        "schema_version": OBSERVATION_SCHEMA,
        "runner": runner,
        "case_id": case_id,
        "run_index": run_index,
        "completion": {
            "status": "completed" if complete else "incomplete",
            "finished": bool(complete),
            "exit_code": runner_exit_code if runner_exit_code is not None else (1 if (not complete or verdict == "FAIL") else 0),
        },
        "artifacts": identity,
        "verdict": verdict,
        "reason": reason,
        "invariants": normalized_invariants,
        "cli_observations": envelope_observations,
        "source_files": source_rows,
        "case_results": normalized_case_results,
        "case_map": case_map_value,
    }
    _write_json(run_dir / "runner-observation.json", envelope)
    return run_dir


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"預期 JSON 物件：{path}")
    return value


def write_json(path: Path, value: Any) -> Path:
    """供測試改寫純資料報告；仍以 write_bytes 保留固定換行。"""
    return _write_json(Path(path), value)


def source_path(report_dir: Path, case_id: str, runner: str, run_index: int, rel: str) -> Path:
    """由側 report-dir 與相對 run 檔定位原始來源。"""
    return Path(report_dir) / case_id / runner / str(run_index) / Path(rel)


def source_sha256(path: Path) -> str:
    """使用 T2 共用替身計算原始檔 SHA-256。"""
    return sha256_file(Path(path))


__all__ = [
    "CASE_ID",
    "CLI_OBSERVATIONS_SCHEMA",
    "OBSERVATION_SCHEMA",
    "RUNNERS",
    "read_json",
    "source_path",
    "source_sha256",
    "write_json",
    "write_run",
]
