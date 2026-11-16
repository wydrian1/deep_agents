"""Daily summary: ``GET /api/summary`` (counts and causes of today's runs, no content)."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from ..auth import current_principal
from ..i18n import message
from ..summary import daily_summary


def router() -> APIRouter:
    api = APIRouter(prefix="/api", dependencies=[Depends(current_principal)], tags=["summary"])

    @api.get("/summary")
    def summary(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        data = daily_summary(request.app.state.engine, ZoneInfo(settings.schedule_timezone), settings.rate_limit_cooldown_seconds)
        for item in data["workflows"]["problems"]:  # stored as a message key; shown in the service language
            error = item.pop("error")
            if not item["reason"] and error:
                head, _, cause = error.partition(":")
                item["reason"] = (message(head, settings.app_language) + (": " + message(cause, settings.app_language) if cause else ""))[:300]
        return data

    return api
