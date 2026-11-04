"""Shared offline fixtures for the application test suite.

No real IMAP/SMTP connection is made. Every service instance uses a temporary
runtime directory, a deterministic fake conversion engine and the in-memory
mail simulator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cater.app.application import AppService
from cater.app.config import AppConfig
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import FakeEngine, make_config


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


@pytest.fixture
def live_config(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path, dry_run=False, enabled=True)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def gateway() -> SimulatorMailGateway:
    return SimulatorMailGateway()


@pytest.fixture
def service_factory():
    created: list[AppService] = []

    def create(config: AppConfig, **kwargs: Any) -> AppService:
        kwargs.setdefault("engine", FakeEngine())
        kwargs.setdefault("gateway", SimulatorMailGateway())
        service = AppService(config, **kwargs)
        created.append(service)
        return service

    yield create
    for service in reversed(created):
        service.close()


@pytest.fixture
def service(service_factory, config: AppConfig, engine, gateway):
    return service_factory(config, engine=engine, gateway=gateway)


@pytest.fixture
def offline_source(tmp_path: Path) -> Path:
    path = tmp_path / "business.xlsx"
    path.write_bytes(b"fake-xlsx-input")
    return path