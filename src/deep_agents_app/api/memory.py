"""Knowledge-base API for the owner: /api/memory (list, read, history, write, delete)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth import current_principal
from ..i18n import message
from ..memory.store import MAX_CHARS, MAX_REVISION, MemoryConflict, MemoryRejected, MemoryStore

OWNER = "owner"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(max_length=MAX_CHARS)
    expected_revision: int = Field(ge=0, lt=MAX_REVISION)
    request_id: str = Field(min_length=1, max_length=200, pattern=_REQUEST_ID.pattern)


class MemoryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, lt=MAX_REVISION)
    request_id: str = Field(min_length=1, max_length=200, pattern=_REQUEST_ID.pattern)


def router() -> APIRouter:
    api = APIRouter(prefix="/api/memory", dependencies=[Depends(current_principal)], tags=["memory"])

    def store_of(request: Request) -> MemoryStore:
        return MemoryStore(request.app.state.engine)

    def language(request: Request) -> str:
        return str(request.app.state.settings.app_language)

    def rejected(request: Request, error: MemoryRejected) -> HTTPException:
        return HTTPException(422, message(error.key, language(request), **error.values))

    def conflict(request: Request, error: MemoryConflict) -> HTTPException:
        return HTTPException(
            409, {"message": message("memory_conflict", language(request)), "current_revision": error.revision}
        )

    @api.get("")
    def list_entries(
        request: Request, prefix: str = "", after: str = "", limit: int = Query(default=50, ge=1, le=50), deleted: bool = False
    ) -> dict[str, Any]:
        try:
            return store_of(request).list_entries(prefix, after, limit, deleted)
        except MemoryRejected as error:
            raise rejected(request, error) from None

    @api.get("/{key}")
    def read_entry(key: str, request: Request, revision: int = Query(default=0, ge=0, lt=MAX_REVISION)) -> dict[str, Any]:
        try:
            return store_of(request).get(key, revision)
        except MemoryRejected as error:
            raise rejected(request, error) from None

    @api.get("/{key}/history")
    def read_history(key: str, request: Request, before: int = Query(default=MAX_REVISION, ge=1, le=MAX_REVISION)) -> dict[str, Any]:
        try:
            return store_of(request).history(key, before)
        except MemoryRejected as error:
            raise rejected(request, error) from None

    @api.put("/{key}")
    def write_entry(key: str, body: MemoryWrite, request: Request) -> dict[str, Any]:
        """Create (expected_revision 0) or change an entry; restoring an old text is a NEW revision."""
        try:
            return store_of(request).put(key, body.value, body.expected_revision, OWNER, body.request_id)
        except MemoryConflict as error:
            raise conflict(request, error) from None
        except MemoryRejected as error:
            raise rejected(request, error) from None

    @api.post("/{key}/delete")
    def delete_entry(key: str, body: MemoryDelete, request: Request) -> dict[str, Any]:
        """Logical delete; the private history stays."""
        try:
            return store_of(request).delete(key, body.expected_revision, OWNER, body.request_id)
        except MemoryConflict as error:
            raise conflict(request, error) from None
        except MemoryRejected as error:
            raise rejected(request, error) from None

    return api
