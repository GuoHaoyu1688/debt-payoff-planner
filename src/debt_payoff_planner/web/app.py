"""Loopback API and static dashboard without CDN or authentication dependencies."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..core.calculations import fee_apr
from ..core.reports import markdown
from ..core.simulation import compare, progress, simulate
from ..core.store import NotFound, Planner


class Payment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    debt_id: str
    amount: str
    date: str
    interest: str | None = None
    note: str = ""
    idempotency_key: str | None = None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str
    budget: str
    start: str | None = None
    months: int = 600


def envelope(data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "warnings": data.get("warnings", []) if isinstance(data, dict) else [],
    }


def create_app(store: Planner) -> FastAPI:
    app = FastAPI(title="Debt Payoff Planner", docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "error": {"code": "FORBIDDEN", "message": "cross-origin writes are blocked"},
                },
                status_code=403,
            )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(NotFound)
    async def missing(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": {"code": "NOT_FOUND", "message": str(exc)}}, status_code=404
        )

    async def invalid(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": str(exc)}},
            status_code=400,
        )

    for exception in (ValueError, sqlite3.IntegrityError, RequestValidationError):
        app.add_exception_handler(exception, invalid)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return envelope(
            {
                "today": store.today().isoformat(),
                "currency": store.currency,
                "default_budget": store.default_budget,
            }
        )

    @app.get("/api/debts")
    def debts() -> dict[str, Any]:
        return envelope(store.list_debts())

    @app.get("/api/compare")
    def comparison(
        budget: str | None = None, start: str | None = None, months: int = 600
    ) -> dict[str, Any]:
        return envelope(compare(store, budget, start, months))

    @app.post("/api/payments")
    def payment(body: Payment) -> dict[str, Any]:
        return envelope(
            store.pay(
                body.debt_id, body.amount, body.date, body.interest, body.note, body.idempotency_key
            )
        )

    @app.get("/api/payments")
    def payment_history() -> dict[str, Any]:
        return envelope(store.payments())

    @app.post("/api/plans")
    def save_plan(body: Plan) -> dict[str, Any]:
        return envelope(
            simulate(store, body.strategy, body.budget, body.start, body.months, save=True)
        )

    @app.get("/api/plans")
    def plans() -> dict[str, Any]:
        return envelope(store.plans())

    @app.get("/api/progress/{plan_id}")
    def plan_progress(plan_id: str, as_of: str | None = None) -> dict[str, Any]:
        return envelope(progress(store, plan_id, as_of))

    @app.get("/api/due")
    def due(days: int = 7) -> dict[str, Any]:
        return envelope(store.due(days))

    @app.get("/api/apr")
    def apr(fee_rate: str, months: int = 12) -> dict[str, Any]:
        return envelope(fee_apr(fee_rate, months))

    @app.get("/api/report")
    def report(
        budget: str | None = None, start: str | None = None, months: int = 600
    ) -> dict[str, Any]:
        return envelope({"markdown": markdown(compare(store, budget, start, months))})

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
