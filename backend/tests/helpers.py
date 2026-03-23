from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import createApp


def createTestClient() -> TestClient:
    return TestClient(createApp())
