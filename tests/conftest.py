from collections.abc import Generator

import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def app() -> FastAPI:
    return create_app()
