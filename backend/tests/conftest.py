from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

backendRoot = Path(__file__).resolve().parents[1]
if str(backendRoot) not in sys.path:
    sys.path.insert(0, str(backendRoot))

from app.main import createApp  # noqa: E402


def createTestClient() -> TestClient:
    return TestClient(createApp())
