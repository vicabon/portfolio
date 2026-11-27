"""共用 fixture：每個測試前後清快取，並預設禁止連網。

測試不可依賴外部報價網站（結果會隨時間變、CI 也可能連不上），所以預設把 requests 與 socket
都換成「一連就拋錯並記錄」的版本；測試結束時若有任何連網嘗試，即使程式內部吞掉了例外，
該測試也會被判為失敗。真的需要連網的測試請標 @pytest.mark.network（預設不跑）。
"""

import os
import socket
import sys

import pytest
import requests

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for path in (ROOT, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import fakes  # noqa: E402
import make_golden  # noqa: E402
import portfolio_data  # noqa: E402

BLOCKED_MESSAGE = "測試不得連網（需要連網的測試請標 @pytest.mark.network）"


class NetworkBlocked(RuntimeError):
    """測試中嘗試連網。"""


@pytest.fixture(autouse=True)
def _isolate(request, monkeypatch):
    portfolio_data.clear_caches()
    portfolio_data.reset_source_stats()
    monkeypatch.setitem(portfolio_data._last_good_pe, "info", None)

    attempts = []
    if request.node.get_closest_marker("network") is None:
        def blocked(*args, **_kwargs):
            attempts.append(repr(args[:2])[:120])
            raise NetworkBlocked(BLOCKED_MESSAGE)

        monkeypatch.setattr(portfolio_data.SESSION, "get", blocked)
        monkeypatch.setattr(socket.socket, "connect", blocked)
        monkeypatch.setattr(socket.socket, "connect_ex", blocked)
        monkeypatch.setattr(socket, "getaddrinfo", blocked)
        monkeypatch.setattr(socket, "create_connection", blocked)

    yield

    portfolio_data.clear_caches()
    if attempts:
        pytest.fail(BLOCKED_MESSAGE + "；嘗試了：" + "; ".join(attempts))


@pytest.fixture
def offline(monkeypatch):
    """模擬所有外部來源都連不上（requests 層級的 ConnectionError，不算違規連網）。"""

    def down(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(portfolio_data.SESSION, "get", down)
    return down


@pytest.fixture
def fake_http(monkeypatch):
    """以假回應取代 SESSION.get；_get() 的狀態碼檢查與來源統計照常執行。"""
    http = fakes.FakeHttp()
    monkeypatch.setattr(portfolio_data.SESSION, "get", http.get)
    return http


@pytest.fixture
def fixed_config():
    """固定門檻（與 golden 相同的 TEST_CONFIG），測試結果不受 config.json 調整影響。"""
    with make_golden.patched_config():
        yield make_golden.TEST_CONFIG
