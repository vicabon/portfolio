"""core.js 對 tests/golden/*.json 必須得到與 Python 相同的結果（需要 node）。

本機沒有 node 時跳過；CI（環境變數 CI 有值）沒有 node 則直接失敗，避免 JS 端默默沒被驗證。
用三個時區各跑一次：日期函式（parseLocalDate、paymentDate、exchangeDateStr）不能依賴執行環境的時區。
"""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join("tests", "js", "run_golden.js")


@pytest.mark.parametrize("tz", ["UTC", "Asia/Taipei", "America/New_York"])
def test_core_js_matches_golden(tz):
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI 必須安裝 node 才能驗證 core.js 與 Python 的一致性")
        pytest.skip("本機沒有 node（CI 會執行這項測試）")
    env = dict(os.environ, TZ=tz)
    proc = subprocess.run(
        [node, RUNNER], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-6000:]
    assert "、0 失敗" in proc.stdout, output[-6000:]
