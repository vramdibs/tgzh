from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from motok_jwt import issue_homework_jwt
from server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_check_requires_jwt_when_secret_set(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("MOTOK_HUB_TOKEN_SECRET", "hub-secret")
    r = client.post(
        "/check",
        files={"photo": ("a.txt", b"x=1", "text/plain")},
        data={"paragraph": "1"},
    )
    assert r.status_code == 401


def test_check_accepts_valid_jwt(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("MOTOK_HUB_TOKEN_SECRET", "hub-secret")
    monkeypatch.setenv("AI_MOCK", "1")
    token = issue_homework_jwt("person-1", secret="hub-secret", ttl_s=60)
    r = client.post(
        "/check",
        files={"photo": ("a.txt", b"x=1", "text/plain")},
        data={"paragraph": "1", "check_id": "cid-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "result" in r.json()
