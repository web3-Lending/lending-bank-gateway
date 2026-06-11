from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.core.context import IdentifierMiddleware


def create_app() -> FastAPI:
    app = FastAPI(title="lending-bank-gateway", version="0.1.0")
    app.add_middleware(IdentifierMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
