import importlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import api, explore, ingest, mcp, page
from .checks import make_client
from .config import Settings

STATIC = Path(__file__).parent / "static"
SCRIPT = STATIC / "p.js"


def load_extensions(settings: Settings) -> list:
    return [importlib.import_module(name) for name in settings.extension_modules]


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = http or make_client()
        yield
        await app.state.http.aclose()
        await engine.dispose()

    app = FastAPI(title="Prodbat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.limiter = api.Limiter()
    # Optional: async (request, project) -> Response | None, asked before a
    # project's page is shown. An extension sets it to put pages behind sign-in.
    app.state.page_guard = None
    # Extensions first, so their routes win where both define one.
    for module in load_extensions(settings):
        module.setup(app)
    for module in (ingest, api, mcp, page, explore):
        app.include_router(module.router)

    @app.get("/p.js")
    async def script() -> FileResponse:
        return FileResponse(
            SCRIPT,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms() -> str:
        return (STATIC / "llms.txt").read_text().replace("{server}", settings.base)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
