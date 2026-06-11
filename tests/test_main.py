from fastapi import FastAPI

from app.main import create_app


def test_create_app_returns_fastapi() -> None:
    assert isinstance(create_app(), FastAPI)
