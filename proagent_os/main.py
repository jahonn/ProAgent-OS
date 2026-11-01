from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from proagent_os import __version__
from proagent_os.api import api_router
from proagent_os.config import get_settings
from proagent_os.db import get_db
from proagent_os.observability import shutdown_observability, trace_http_request
from proagent_os.oidc import require_auth_config
from proagent_os.ratelimit import enforce_rate_limit
from proagent_os.services import get_services

WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_db()
    get_services()
    yield
    shutdown_observability()


def create_app() -> FastAPI:
    settings = get_settings()
    # oidc without issuer, audience, and client id raises before the app exists.
    require_auth_config(settings)
    app = FastAPI(
        title="ProAgent OS",
        description="Vertical-agnostic Pro-Agent framework (Professional / Proactive / Profitable).",
        version=__version__,
        lifespan=lifespan,
    )

    app.include_router(api_router)
    # Last added middleware runs first. Tracing wraps the rate limit so a 429
    # is still a span when OpenTelemetry is enabled. Disabled, this is a no-op
    # and does not import opentelemetry.
    app.middleware("http")(enforce_rate_limit)
    app.middleware("http")(trace_http_request)

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "llm": settings.llm_provider,
            "auth_mode": settings.auth_mode,
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "admin_hint": settings.admin_key[:4] + "…" if settings.admin_key else "",
            },
        )

    return app


app = create_app()
