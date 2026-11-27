"""Python 端目前的輸出必須等於 tests/golden/*.json（core.js 端由 test_js_golden.py 驗證）。"""

import json
import os
import re

import pytest

import make_golden


def _load(name):
    with open(make_golden.golden_path(name), encoding="utf-8") as handle:
        return json.load(handle)


def core_exports():
    """core.js 最後的 return { ... }; 匯出清單。"""
    with open(os.path.join(make_golden.ROOT, "core.js"), encoding="utf-8") as handle:
        text = handle.read()
    start = text.rindex("    return {")
    end = text.index("};", start)
    body = re.sub(r"//[^\n]*", "", text[start + len("    return {"):end])
    return {item.strip() for item in body.split(",") if item.strip()}


@pytest.mark.parametrize("name", sorted(make_golden.BUILDERS))
def test_python_matches_golden(name):
    problems = make_golden.compare(make_golden.build(name), _load(name))
    assert not problems, (
        "Python 輸出與 golden 不一致。若是刻意修改規則：執行 python tests/make_golden.py 重新產生，"
        "並同步修改 core.js。\n" + "\n".join(problems[:20])
    )


def test_golden_files_match_builders():
    files = {f[:-5] for f in os.listdir(make_golden.GOLDEN_DIR) if f.endswith(".json")}
    assert files == set(make_golden.BUILDERS), "多出或缺少 golden 檔：" + repr(files ^ set(make_golden.BUILDERS))


def test_golden_cases_are_well_formed():
    allowed = {"name", "js", "args", "expected", "throws", "tol", "argTypes", "resultType"}
    for name in make_golden.BUILDERS:
        payload = _load(name)
        assert payload["cases"], name + " 沒有任何 case"
        keys = [c["js"] + " / " + c["name"] for c in payload["cases"]]
        assert len(keys) == len(set(keys)), name + " 有重複的 case 名稱"
        for c in payload["cases"]:
            assert set(c) <= allowed, (name, c["name"], set(c) - allowed)
            assert ("expected" in c) != bool(c.get("throws")), (name, c["name"])
            assert isinstance(c["args"], list), (name, c["name"])


def test_golden_functions_are_exported_by_core_js():
    exported = core_exports()
    assert {"resolveConsensus", "computeLohas", "summarizePositions", "amortizeToToday"} <= exported
    used = {c["js"] for name in make_golden.BUILDERS for c in _load(name)["cases"]}
    assert not used - exported, "golden 用到 core.js 沒匯出的函式：" + ", ".join(sorted(used - exported))

