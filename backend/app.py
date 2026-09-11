"""FastAPI backend for the Airbus maintenance agent UI.

Holds the Azure credential so no token ever reaches the browser, and converts the
agent's OpenAI Responses stream into the UI's trace-event vocabulary.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from trace import translate

logger = logging.getLogger("airbus-agent-backend")

FOUNDRY_SCOPE = "https://ai.azure.com/.default"


class ChatRequest(BaseModel):
    message: str
    previous_response_id: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.credential = None if settings.is_local else DefaultAzureCredential()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
    try:
        yield
    finally:
        await app.state.http.aclose()
        if app.state.credential is not None:
            await app.state.credential.close()


app = FastAPI(title="Airbus maintenance agent", lifespan=lifespan)

# The Vite dev server proxies /api, so this only matters if the UI is served from a
# different origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _responses_url() -> str:
    if settings.is_local:
        return f"{settings.local_agent_url.rstrip('/')}/responses"
    return (
        f"{settings.project_endpoint.rstrip('/')}"
        f"/agents/{settings.agent_name}/endpoint/protocols/openai/responses"
        "?api-version=v1"
    )


async def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not settings.is_local:
        token = await app.state.credential.get_token(FOUNDRY_SCOPE)
        headers["Authorization"] = f"Bearer {token.token}"
    return headers


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _stream_run(request: ChatRequest) -> AsyncIterator[str]:
    body: dict[str, Any] = {"input": request.message, "stream": True}
    if request.previous_response_id:
        body["previous_response_id"] = request.previous_response_id

    try:
        async with app.state.http.stream(
            "POST", _responses_url(), headers=await _headers(), json=body
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")[:600]
                logger.error("Agent returned %s: %s", response.status_code, detail)
                yield _sse(
                    {
                        "type": "run.error",
                        "data": {
                            "message": f"Agent returned HTTP {response.status_code}",
                            "detail": detail,
                        },
                    }
                )
                return

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw.strip() == "[DONE]":
                    continue
                try:
                    # strict=False: manual passages carry raw control characters.
                    event = json.loads(raw, strict=False)
                except json.JSONDecodeError:
                    continue
                try:
                    for translated in translate(event):
                        yield _sse(translated)
                except Exception:
                    # A shape we didn't anticipate must not kill the whole run --
                    # forward it raw and keep streaming.
                    logger.exception("Failed to translate event %s", event.get("type"))
                    yield _sse({"type": "raw", "data": {"event_type": event.get("type")}})
    except httpx.HTTPError as exc:
        logger.exception("Transport error talking to the agent")
        yield _sse({"type": "run.error", "data": {"message": f"Could not reach the agent: {exc}"}})


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_run(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    target = "local" if settings.is_local else "foundry"
    info: dict[str, Any] = {
        "target": target,
        "agent": settings.agent_name,
        "model": settings.model,
        "endpoint": _responses_url(),
    }
    if settings.is_local:
        info["reachable"] = True
        return info

    # Confirm the agent exists and report which version the endpoint routes to.
    url = f"{settings.project_endpoint.rstrip('/')}/agents/{settings.agent_name}?api-version=v1"
    try:
        response = await app.state.http.get(url, headers=await _headers())
        info["reachable"] = response.status_code == 200
        if response.status_code == 200:
            agent = response.json()
            info["version"] = (agent.get("versions") or {}).get("latest", {}).get("version")
            info["state"] = agent.get("state")
        else:
            info["error"] = response.text[:300]
    except httpx.HTTPError as exc:
        info["reachable"] = False
        info["error"] = str(exc)
    return info


# --- Built frontend -----------------------------------------------------------
# In development the Vite dev server serves the UI and proxies /api here, so this
# block does nothing. In the container the built assets sit next to the backend and
# are served from the same origin, which keeps cookies (including the Entra auth
# cookie) and SSE on one host with no CORS.
WEB_DIST = Path(os.getenv("WEB_DIST", Path(__file__).resolve().parent.parent / "web" / "dist"))

if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        """Serve index.html for any non-API path so client-side routing works."""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve a real file when one exists (favicon, robots.txt), else the SPA shell.
        candidate = WEB_DIST / full_path
        if full_path and candidate.is_file() and WEB_DIST in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
else:
    logger.info("No built frontend at %s - serving API only.", WEB_DIST)
