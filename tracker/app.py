"""FastAPI app: REST + Server-Sent Events + static dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .capture import Broadcaster, CaptureService
from .config import Config
from .db import DB
from .notifier import Notifier, Sender
from .session import CdpWatcher

log = logging.getLogger("tracker.app")
STATIC = Path(__file__).parent / "static"


class SettingsIn(BaseModel):
    selected_locations: list[str] | None = None
    target_before: str | None = None
    current_ofc_date: str | None = None
    current_interview_date: str | None = None
    daily_view_budget: int | None = None
    alerts_enabled: bool | None = None

    @field_validator("target_before", "current_ofc_date", "current_interview_date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        return date.fromisoformat(v).isoformat()

    @field_validator("daily_view_budget")
    @classmethod
    def _budget(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 50:
            raise ValueError("budget must be 1..50")
        return v


def create_app(cfg: Config, *, db: DB | None = None, sender: Sender | None = None,
               start_watcher: bool = True) -> FastAPI:
    db = db or DB(cfg.db_path)
    bus = Broadcaster()
    svc = CaptureService(cfg, db, sender or Notifier(cfg), bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watcher: CdpWatcher | None = None
        task: asyncio.Task[None] | None = None
        if start_watcher:
            watcher = CdpWatcher(cfg.cdp_url, cfg.site_host, cfg.login_page_pattern,
                                 svc.on_exchange, svc.on_chrome_state)
            task = asyncio.create_task(watcher.run())
            log.info("Passive capture started; watching %s via %s", cfg.site_host, cfg.cdp_url)
        try:
            yield
        finally:
            if watcher:
                await watcher.stop()
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            db.close()
            log.info("Shut down cleanly")

    app = FastAPI(title="Visa slot tracker (passive)", lifespan=lifespan)
    app.state.svc = svc

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/status")
    def status() -> dict[str, Any]:
        return svc.status()

    @app.get("/history")
    def history(location: str | None = None, days: int = Query(30, ge=1, le=365)) -> list[dict[str, Any]]:
        return db.history(location, days)

    @app.get("/slots")
    def slots(location: str, appt_type: str, date: str) -> dict[str, Any]:
        if appt_type not in ("OFC", "INTERVIEW"):
            raise HTTPException(400, "appt_type must be OFC or INTERVIEW")
        return db.latest_time_slots(location, appt_type, date) or {"captured_at": None, "times": []}

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return svc.settings()

    @app.post("/settings")
    def post_settings(body: SettingsIn) -> dict[str, Any]:
        data = body.model_dump(exclude_unset=True)
        return svc.update_settings(data)

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        async def gen() -> AsyncIterator[str]:
            try:
                yield f"event: status\ndata: {json.dumps(svc.status())}\n\n"
                while not await request.is_disconnected():
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
