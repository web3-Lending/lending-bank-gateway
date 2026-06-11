from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.core.config import get_settings
from app.core.context import IdentifierMiddleware
from app.core.db import build_engine, build_session_factory
from app.core.s2s import S2SMiddleware


def create_app() -> FastAPI:
    app = FastAPI(title="lending-bank-gateway", version="0.1.0")
    settings = get_settings()
    # starlette add_middleware 是栈式：后 add 先执行
    # 执行顺序：IdentifierMiddleware → S2SMiddleware → handler
    app.add_middleware(
        S2SMiddleware, secret=settings.s2s_secret, exempt_paths={"/healthz", "/readyz"}
    )
    app.add_middleware(IdentifierMiddleware)
    engine = build_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.include_router(health_router)
    return app


app = create_app()
