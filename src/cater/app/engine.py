"""Lazy adapter to the business core's public JSON contract.

The application must not import ``cater.core`` at module import time: the core
is developed concurrently, and the app must remain runnable/testable with an
injected fake engine until it lands. The app never creates placeholder core
files.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Callable, Protocol

CORE_TEMPLATE_FILES = (
    "parts_ledger.xlsx",
    "machine_materials.xlsx",
    "machine_comm.xlsx",
)


class CoreUnavailable(RuntimeError):
    pass


def ensure_core_templates() -> Path:
    """Fail clearly before conversion when the core templates are absent.

    The core resolves ``templates/`` relative to the source checkout by
    default. Normal wheel installs therefore do not contain usable templates;
    CATER documents source-checkout deployment only. An explicit
    ``CATER_TEMPLATE_DIR`` may point at a directory containing the three
    workbook templates for advanced deployments.
    """

    override = os.environ.get("CATER_TEMPLATE_DIR")
    if override:
        directory = Path(override).expanduser()
    else:
        try:
            module = importlib.import_module("cater.core.api")
        except ModuleNotFoundError as exc:
            if exc.name in {"cater.core", "cater.core.api"}:
                raise CoreUnavailable(
                    "cater.core.api is not available; install the core from the source checkout"
                ) from exc
            raise
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise CoreUnavailable("cater.core.api has no source file; cannot locate templates")
        directory = Path(module_file).resolve().parents[3] / "templates"
    missing = [name for name in CORE_TEMPLATE_FILES if not (directory / name).is_file()]
    if missing:
        raise CoreUnavailable(
            "Core Excel templates are missing at "
            f"{directory}: {', '.join(missing)}. CATER supports source-checkout "
            "editable installs only; do not use a normal wheel. Set "
            "CATER_TEMPLATE_DIR to a directory containing all three templates "
            "for an advanced deployment."
        )
    return directory


class ProcessEngine(Protocol):
    def process_job(self, request: dict[str, Any]) -> dict[str, Any]: ...


class CoreProcessEngine:
    def __init__(self) -> None:
        self._process_job: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @staticmethod
    def _load() -> Callable[[dict[str, Any]], dict[str, Any]]:
        try:
            module = importlib.import_module("cater.core.api")
        except ModuleNotFoundError as exc:
            if exc.name in {"cater.core", "cater.core.api"}:
                raise CoreUnavailable(
                    "cater.core.api is not available yet; inject a test engine or install the core"
                ) from exc
            raise
        function = getattr(module, "process_job", None)
        if not callable(function):
            raise CoreUnavailable("cater.core.api.process_job is missing or not callable")
        return function

    def process_job(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._process_job is None:
            self._process_job = self._load()
        ensure_core_templates()
        return self._process_job(request)


def load_import_machine_reference() -> Callable[[str], list[dict[str, Any]]]:
    try:
        module = importlib.import_module("cater.core.api")
    except ModuleNotFoundError as exc:
        if exc.name in {"cater.core", "cater.core.api"}:
            raise CoreUnavailable(
                "cater.core.api is not available yet; machine reference import requires the core"
            ) from exc
        raise
    function = getattr(module, "import_machine_reference", None)
    if not callable(function):
        raise CoreUnavailable("cater.core.api.import_machine_reference is missing or not callable")
    return function