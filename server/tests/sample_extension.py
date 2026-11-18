"""A stand-in extension for the tests: one route that wins over the server's
own, a page guard, and a tick for the worker."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter()
ticks: list = []


@router.get("/llms.txt", response_class=PlainTextResponse)
async def llms() -> str:
    return "from the extension"


async def guard(request, project):
    if request.headers.get("x-signed-in") != "yes":
        raise HTTPException(303, headers={"Location": "/login"})


def setup(app) -> None:
    app.include_router(router)
    app.state.page_guard = guard


async def tick(sessions, settings, http) -> None:
    ticks.append(settings.base)
