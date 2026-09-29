"""Tests never call a real model.

llm.api_key() also reads a local .env, so removing GROQ_API_KEY from the
environment is not enough: a developer's real key would be picked up and tests
would spend it. AI_PROVIDER=off makes llm.available() false for every test,
including the ones that start a subprocess, since those copy os.environ. Tests
that need a model patch llm.available or pass a scripted model directly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "off")
