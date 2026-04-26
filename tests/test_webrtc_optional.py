from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.api import server as srv


def test_webrtc_offer_returns_501_when_optional_deps_missing(monkeypatch):
    monkeypatch.setattr(srv, "_webrtc_manager", None)
    monkeypatch.setattr(srv, "_webrtc_import_error", ImportError("No module named 'aiortc'"))

    with TestClient(srv.app, raise_server_exceptions=False) as client:
        response = client.post("/webrtc/offer", json={"sdp": "offer", "type": "offer"})

    assert response.status_code == 501
    payload = response.json()
    assert "install optional dependencies" in payload["error"].lower()
    assert "aiortc av" in payload["error"]


def test_webrtc_offer_uses_manager_when_available(monkeypatch):
    handle_offer = AsyncMock(return_value={"sdp": "answer", "type": "answer"})
    monkeypatch.setattr(srv, "_webrtc_manager", SimpleNamespace(handle_offer=handle_offer))
    monkeypatch.setattr(srv, "_webrtc_import_error", None)

    with TestClient(srv.app, raise_server_exceptions=False) as client:
        response = client.post("/webrtc/offer", json={"sdp": "offer", "type": "offer"})

    assert response.status_code == 200
    assert response.json() == {"sdp": "answer", "type": "answer"}
    handle_offer.assert_awaited_once_with("offer", "offer")