"""Foundry IQ knowledge base access for the hosted agent.

The agent queries the knowledge base through its REST ``/retrieve`` endpoint rather
than its MCP endpoint. Both are supported ways to query a Foundry IQ knowledge base,
but only ``/retrieve`` returns the ``activity`` array describing how the answer was
assembled: which model planned the query, how it decomposed the question into
subqueries, and how long each one took. That detail is what the trace UI renders, so
it is worth giving up the MCP tool's turnkey wiring to get it.
"""

from __future__ import annotations

import json
import os
import time
from typing import Annotated, Any

import httpx
from agent_framework import tool
from azure.core.credentials_async import AsyncTokenCredential

SEARCH_SCOPE = "https://search.azure.com/.default"

# The retrieved passages dominate the agent's token cost, so they are capped. A single
# retrieve call against the A320 manuals routinely returns 25+ passages of 2 KB each;
# at these defaults the tool output lands around 25 KB (~6.5k tokens). Lower them to
# trade answer completeness for cost.
MAX_PASSAGES = int(os.getenv("KB_MAX_PASSAGES", "12"))
MAX_PASSAGE_CHARS = int(os.getenv("KB_MAX_PASSAGE_CHARS", "1800"))


class KnowledgeBaseClient:
    """Calls the Foundry IQ knowledge base with the agent's own Entra identity."""

    def __init__(
        self,
        *,
        search_endpoint: str,
        knowledge_base: str,
        credential: AsyncTokenCredential,
        api_version: str,
    ) -> None:
        self._url = (
            f"{search_endpoint.rstrip('/')}/knowledgeBases/{knowledge_base}"
            f"/retrieve?api-version={api_version}"
        )
        self._credential = credential
        # Agentic retrieval against the manuals routinely takes 30-60s and has been
        # observed near 50s, so the read timeout is deliberately generous.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(float(os.getenv("KB_TIMEOUT_SECONDS", "180")), connect=10.0)
        )
        self._token: str | None = None
        self._token_expires_on: float = 0.0

    async def _bearer(self) -> str:
        # Refresh a couple of minutes early so a long retrieval can't expire mid-flight.
        if self._token is None or time.time() >= self._token_expires_on - 120:
            token = await self._credential.get_token(SEARCH_SCOPE)
            self._token = token.token
            self._token_expires_on = token.expires_on
        return self._token

    async def retrieve(self, query: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {await self._bearer()}",
            "Content-Type": "application/json",
        }
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": query}]}]}
        response = await self._http.post(self._url, headers=headers, json=body)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._http.aclose()


def _passages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the extractive passages out of the knowledge base response.

    The knowledge base runs in ``extractiveData`` output mode, so ``response`` carries
    a JSON-encoded array of ``{ref_id, content}`` inside a text content block rather
    than prose. Our own model does the synthesis.
    """
    passages: list[dict[str, Any]] = []
    for message in payload.get("response", []):
        for block in message.get("content", []):
            if block.get("type") != "text":
                continue
            try:
                items = json.loads(block.get("text") or "[]")
            except json.JSONDecodeError:
                # Answer-synthesis mode returns prose instead of a JSON array.
                passages.append({"ref_id": None, "content": block.get("text", "")})
                continue
            if isinstance(items, list):
                passages.extend(item for item in items if isinstance(item, dict))
    return passages


def _references(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise the reference list.

    ``sourceData`` comes back null from the REST retrieve endpoint, so the useful
    identity is ``docName`` plus ``citationUrl``. ``activitySource`` is the id of the
    subquery that surfaced the document, which lets the UI attribute each citation to
    the search that found it.
    """
    references = []
    for reference in payload.get("references", []):
        references.append(
            {
                "ref_id": reference.get("id"),
                "type": reference.get("type"),
                "doc_name": reference.get("docName"),
                "score": reference.get("rerankerScore"),
                "activity_source": reference.get("activitySource"),
                "citation_url": reference.get("citationUrl"),
            }
        )
    return references


def _activity(payload: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    """Flatten the knowledge base's ``activity`` array into a compact trace record.

    The knowledge base emits several activity kinds: one or more ``modelQueryPlanning``
    steps (it re-plans after seeing first-round results), one entry per executed
    knowledge-source query, and a final ``agenticReasoning`` summary carrying the
    billed reasoning tokens.
    """
    planning: list[dict[str, Any]] = []
    subqueries: list[dict[str, Any]] = []
    reasoning: dict[str, Any] = {}

    for step in payload.get("activity", []):
        kind = step.get("type")

        if kind == "modelQueryPlanning":
            planning.append(
                {
                    "id": step.get("id"),
                    "model": (step.get("model") or {}).get("modelName"),
                    "input_tokens": step.get("inputTokens"),
                    "output_tokens": step.get("outputTokens"),
                    "elapsed_ms": step.get("elapsedMs"),
                }
            )
        elif kind == "agenticReasoning":
            reasoning = {
                "reasoning_tokens": step.get("reasoningTokens"),
                "retrieval_effort": (step.get("retrievalReasoningEffort") or {}).get("kind"),
                "logical_effort": (step.get("logicalReasoningEffort") or {}).get("kind"),
            }
        else:
            # Every remaining kind is a knowledge-source query. The argument bag is
            # named after the source kind (fileArguments, searchIndexArguments, ...),
            # so find whichever *Arguments key is present rather than hard-coding one.
            arguments = next(
                (
                    value
                    for key, value in step.items()
                    if key.endswith("Arguments") and isinstance(value, dict)
                ),
                {},
            )
            subqueries.append(
                {
                    "id": step.get("id"),
                    "knowledge_source": step.get("knowledgeSourceName"),
                    "kind": kind,
                    "search": arguments.get("search") or arguments.get("query"),
                    "count": step.get("count"),
                    "elapsed_ms": step.get("elapsedMs"),
                }
            )

    return {
        "planning": planning,
        "subqueries": subqueries,
        "reasoning": reasoning,
        "total_elapsed_ms": elapsed_ms,
        "reference_count": len(payload.get("references", [])),
    }


def build_search_tool(client: KnowledgeBaseClient):
    """Build the retrieval tool bound to ``client``."""

    @tool(name="search_airplane_knowledge")
    async def search_airplane_knowledge(
        query: Annotated[
            str,
            "The user's information need as one complete, natural-language question. "
            "Keep every distinctive token from the user verbatim: aircraft types (A320, "
            "A320neo), part names, ATA chapter numbers, figure identifiers and "
            "measurements. Do not reduce it to keywords.",
        ],
    ) -> str:
        """Search the Airbus airplane knowledge base for manuals, parts and procedures.

        Returns extractive passages from the source documents together with the
        references they came from. Always call this before answering; it is the only
        authoritative source for aircraft questions.
        """
        started = time.monotonic()
        try:
            payload = await client.retrieve(query)
        except httpx.HTTPStatusError as exc:
            # Surfaced to the model so it can say it couldn't search, and to the trace
            # panel so the cause is visible rather than looking like an empty corpus.
            return json.dumps(
                {
                    "error": f"Knowledge base returned HTTP {exc.response.status_code}",
                    "detail": exc.response.text[:500],
                    "passages": [],
                    "references": [],
                }
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        passages = [
            {"ref_id": p.get("ref_id"), "content": (p.get("content") or "")[:MAX_PASSAGE_CHARS]}
            for p in _passages(payload)[:MAX_PASSAGES]
        ]
        # Only hand back references for passages the model actually received, so it
        # can never cite a ref_id it hasn't seen. Compared as strings because the
        # passages carry ints and the reference list carries strings.
        kept = {str(p["ref_id"]) for p in passages if p.get("ref_id") is not None}
        references = [r for r in _references(payload) if str(r.get("ref_id")) in kept]

        return json.dumps(
            {
                "passages": passages,
                "references": references,
                # Diagnostic only -- the trace UI reads this back out of the streamed
                # tool output. Never treat it as source content.
                "retrieval_activity": _activity(payload, elapsed_ms),
            }
        )

    return search_airplane_knowledge


def client_from_env(credential: AsyncTokenCredential) -> KnowledgeBaseClient:
    return KnowledgeBaseClient(
        search_endpoint=os.environ["SEARCH_ENDPOINT"],
        knowledge_base=os.environ["KB_NAME"],
        credential=credential,
        api_version=os.getenv("KB_API_VERSION", "2026-08-01-preview"),
    )
