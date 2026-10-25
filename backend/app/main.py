import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, db, notify, operations, payments, requester_api, trust, web
from .body_limits import BodyLimitMiddleware
from .config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.open_pool(settings.database_url)
    if settings.schema_auto_apply:
        with db.transaction() as conn:
            db.init_schema(conn)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    worker = None
    if settings.email_worker_enabled:
        worker = notify.OutboxWorker(app.state.mailer, app.state.pusher)
        worker.start()
    yield
    if worker:
        worker.stop()
    db.close_pool()


def create_app() -> FastAPI:
    # Uvicorn only configures its own loggers; this surfaces the app's (including console-mode emails).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
    settings = get_settings()
    settings.check_safe_for_environment()
    app = FastAPI(title="Paylee", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)
    app.state.payments = payments.build_provider(settings)
    app.state.mailer = notify.build_mailer(settings)
    app.state.pusher = notify.build_pusher(settings)
    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
    app.include_router(api.router)
    app.include_router(web.router)
    app.include_router(operations.router)
    app.include_router(requester_api.router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/api/requests"):
            fields = {str(item["loc"][-1]): item["msg"] for item in error.errors()}
            return JSONResponse(status_code=422, content={"detail": fields})
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(trust.TrustError)
    async def trust_error(request: Request, error: trust.TrustError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def duplicate_reference(request: Request, error):
        return JSONResponse(status_code=409, content={"detail": "This reference already exists; reconcile the original record before retrying"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/admin", "/api")):
            response.headers["Cache-Control"] = "no-store"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
