"""A small Model Context Protocol endpoint, so a coding agent can ask
Prodbat about a site without the CLI.

POST /mcp speaks JSON-RPC over plain HTTP responses (no streaming, no
sessions). Every tool except prodbat_check needs the project's secret key
as a Bearer token.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import api, tasks

router = APIRouter()
PROTOCOL = "2025-03-26"
NO_ARGS = {"type": "object", "properties": {}}

ID_ARG = {"type": "object", "properties": {"id": {"type": "string", "description": "A task id, such as T12 or S-01"}},
          "required": ["id"]}

TOOLS = [
    {"name": "prodbat_next_task",
     "description": "The most important thing to do for the live site right now, as one task with exact steps "
                    "and the evidence. Call this at the start of a session and when asked what to work on.",
     "inputSchema": NO_ARGS},
    {"name": "prodbat_tasks",
     "description": "Every open task for the live site, most important first.",
     "inputSchema": NO_ARGS},
    {"name": "prodbat_task",
     "description": "One task in full, by id.",
     "inputSchema": ID_ARG},
    {"name": "prodbat_check_task",
     "description": "Call this when you have finished a task and deployed. Prodbat looks again from its own "
                    "side and says whether it is fixed.",
     "inputSchema": ID_ARG},
    {"name": "prodbat_status",
     "description": "What is wrong with the live site right now, with the evidence needed to fix it. "
                    "Call this first when asked what is broken in production.",
     "inputSchema": NO_ARGS},
    {"name": "prodbat_verify",
     "description": "Check from outside that the site loads and that Prodbat is installed and receiving "
                    "data. Call this after every deploy.",
     "inputSchema": NO_ARGS},
    {"name": "prodbat_check",
     "description": "A one-off look at any public site from outside: does it load, is the secure connection "
                    "good, when does the domain run out. Needs no key.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "yourapp.com"}},
                     "required": ["url"]}},
    {"name": "prodbat_suggestions",
     "description": "List further things Prodbat could watch on this project.",
     "inputSchema": NO_ARGS},
    {"name": "prodbat_suggestion",
     "description": "The exact steps for one suggestion, by id (for example S-01).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "prodbat_test_alert",
     "description": "Send the owner a test alert so they can see what a real one looks like.",
     "inputSchema": NO_ARGS},
]


async def call(request: Request, name: str, args: dict) -> str:
    state = request.app.state
    if name == "prodbat_check":
        state.limiter.check("check", api.caller_ip(request), 10, 60)
        return (await api.free_check(state.http, str(args.get("url", ""))))["text"]
    async with state.sessions() as db:
        project = await api.project_for(request, db)
        if name == "prodbat_next_task":
            return (await tasks.next_task(db, project, state.settings))["text"]
        if name == "prodbat_tasks":
            return (await tasks.tasks_text(db, project, state.settings))["text"]
        if name == "prodbat_task":
            return tasks.render_task(await tasks.get_task(db, project, state.settings, str(args.get("id", ""))))
        if name == "prodbat_check_task":
            state.limiter.check("task-check", str(project.id), 60, 60)
            return (await tasks.check_task(db, project, state.settings, state.http, str(args.get("id", ""))))["text"]
        if name == "prodbat_status":
            return (await api.status(db, project, state.settings))["text"]
        if name == "prodbat_verify":
            return (await api.verify(db, project, state.settings, state.http))["text"]
        if name == "prodbat_suggestions":
            return (await api.suggestions(db, project, state.settings))["text"]
        if name == "prodbat_suggestion":
            return (await api.suggestions(db, project, state.settings, str(args.get("id", ""))))["text"]
        if name == "prodbat_test_alert":
            state.limiter.check("test-alert", str(project.id), 5, 3600)
            queued = api.queue_test_alert(db, project, state.settings)
            await db.commit()
            return (f"A test alert is on its way to {api.test_destinations(project, state.settings)}." if queued
                    else api.NOWHERE)
    raise KeyError(name)


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if not isinstance(message, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "Send one request at a time."}})
    method, rid = message.get("method"), message.get("id")
    if rid is None:  # a notification, such as notifications/initialized
        return Response(status_code=202)

    def result(value: dict) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": value})

    if method == "initialize":
        return result({"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "prodbat", "version": "0.0.1"}})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            text = await call(request, params.get("name", ""), params.get("arguments") or {})
        except KeyError:
            return JSONResponse({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}})
        except HTTPException as exc:
            # The tool ran and has something to say (bad key, bad input): that
            # is a tool result the agent should read, not a protocol error.
            return result({"content": [{"type": "text", "text": str(exc.detail)}], "isError": True})
        return result({"content": [{"type": "text", "text": text}]})
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown method: {method}"}})
