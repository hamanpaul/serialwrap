#!/usr/bin/env python3
"""從 pinned Python 基線的實際 TestCheckSource 輸入產生 Rust parity golden。"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import importlib.util
import inspect
import json
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE_REF = "origin/main"
BASELINE_COMMIT = "d453bbf023d21baeb3aa96ced3357854b458ade9"
BASELINE_LABEL = f"{BASELINE_REF}@{BASELINE_COMMIT}"
SOURCE_PATH = "sw_core/source_policy.py"
TEST_PATH = "tests/test_source_policy.py"
OUTPUT_PATH = ROOT / "contracts/golden/p5/source_policy.ndjson"
TEST_INPUT_CONSTANTS = {"EXISTING_SOURCE_VALUES", "EXISTING_HUMAN_VALUES"}


def git_show(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def load_python_baseline(source: str):
    name = "_source_policy_python_baseline"
    spec = importlib.util.spec_from_loader(name, loader=None)
    if spec is None:
        raise RuntimeError("無法建立 Python 基線模組")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, f"{BASELINE_LABEL}:{SOURCE_PATH}", "exec"), module.__dict__)
    return module


def test_source_nodes(source: str) -> list[ast.stmt]:
    tree = ast.parse(source, filename=f"{BASELINE_LABEL}:{TEST_PATH}")
    selected: list[ast.stmt] = []
    found_constants: set[str] = set()
    found_test_class = False

    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name) and target.id in TEST_INPUT_CONSTANTS
            }
            if names:
                selected.append(copy.deepcopy(node))
                found_constants.update(names)
        elif isinstance(node, ast.ClassDef) and node.name == "TestCheckSource":
            test_class = copy.deepcopy(node)
            test_class.bases = [ast.Name(id="RecordingTestCase", ctx=ast.Load())]
            test_class.decorator_list = []
            selected.append(test_class)
            found_test_class = True

    missing = TEST_INPUT_CONSTANTS - found_constants
    if missing or not found_test_class:
        raise RuntimeError(
            f"Python 測試表結構變更，找不到常數 {sorted(missing)} 或 TestCheckSource"
        )
    return selected


def capture_rows(source_module, test_source: str) -> list[dict[str, object]]:
    active_test = ""
    captured: list[dict[str, object]] = []
    original_check = source_module.check_source

    class RecordingTestCase(unittest.TestCase):
        def run(self, result=None):
            nonlocal active_test
            previous = active_test
            active_test = self._testMethodName
            try:
                return super().run(result)
            finally:
                active_test = previous

    def tracked_check_source(raw, *args, **kwargs):
        bound = inspect.signature(original_check).bind(raw, *args, **kwargs)
        bound.apply_defaults()
        result = original_check(raw, *args, **kwargs)
        captured.append(
            {
                "test": active_test,
                "input": {
                    "raw": bound.arguments["raw"],
                    "default": bound.arguments["default"],
                    "field": bound.arguments["field"],
                    "normalize_human": bound.arguments["normalize_human"],
                },
                "source": result.source,
                "normalized_from": result.normalized_from,
                "raw_source": result.raw_source,
                "is_bare_human": result.is_bare_human,
                "source_class": source_module.classify_source(result.source),
                "error": result.error,
            }
        )
        return result

    namespace = {
        "RecordingTestCase": RecordingTestCase,
        "check_source": tracked_check_source,
        "classify_source": source_module.classify_source,
        "ERROR_SOURCE_RESERVED": source_module.ERROR_SOURCE_RESERVED,
        "SOURCE_CLASS_AGENT": source_module.SOURCE_CLASS_AGENT,
        "SOURCE_CLASS_HUMAN": source_module.SOURCE_CLASS_HUMAN,
    }
    module = ast.Module(body=test_source_nodes(test_source), type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), f"{BASELINE_REF}:{TEST_PATH}", "exec"), namespace)

    test_class = namespace["TestCheckSource"]
    cases = unittest.TestSuite(
        test_class(name)
        for name in sorted(name for name in dir(test_class) if name.startswith("test_"))
    )
    result = unittest.TestResult()
    cases.run(result)
    if result.errors or result.failures or result.testsRun == 0:
        details = "\n".join(
            [f"{test}: {error}" for test, error in result.errors + result.failures]
        )
        raise RuntimeError(f"Python TestCheckSource 基線未通過：\n{details}")
    if not captured:
        raise RuntimeError("TestCheckSource 沒有執行任何 check_source 輸入")
    return captured


def render(rows: list[dict[str, object]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=False, separators=(",", ":")) + "\n"
        for row in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只比對，不覆寫 golden")
    args = parser.parse_args()

    subprocess.run(
        ["git", "cat-file", "-e", f"{BASELINE_COMMIT}^{{commit}}"],
        cwd=ROOT,
        check=True,
    )

    source_module = load_python_baseline(git_show(SOURCE_PATH))
    rows = capture_rows(source_module, git_show(TEST_PATH))
    generated = render(rows)

    if args.check:
        if not OUTPUT_PATH.is_file():
            print(f"golden 不存在：{OUTPUT_PATH.relative_to(ROOT)}", file=sys.stderr)
            return 1
        existing = OUTPUT_PATH.read_text(encoding="utf-8")
        if existing != generated:
            diff = difflib.unified_diff(
                existing.splitlines(),
                generated.splitlines(),
                fromfile=str(OUTPUT_PATH.relative_to(ROOT)),
                tofile="Python 產生結果",
                lineterm="",
            )
            print("\n".join(diff), file=sys.stderr)
            return 1
        print(f"PASS：{len(rows)} 筆 Python TestCheckSource 輸入與 golden 相同")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(generated, encoding="utf-8")
    print(f"已產生 {OUTPUT_PATH.relative_to(ROOT)}（{len(rows)} 筆）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"capture_source_policy_golden.py: {error}", file=sys.stderr)
        raise SystemExit(1) from error
