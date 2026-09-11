"""Translate OpenAI Responses stream events into the UI's trace-event vocabulary.

The browser never sees raw Responses events. This module is the single place that
knows their shape, so the React app stays stable while the preview API moves. Events
it doesn't recognise are forwarded as ``raw`` rather than dropped -- an unfamiliar
event should show up in the trace panel as something unexplained, not vanish.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator


def _event(event_type: str, /, **data: Any) -> dict[str, Any]:
    # Positional-only so payload fields named "kind"/"type" can't collide with it.
    return {"type": event_type, "ts": time.time(), "data": data}


def _split_tool_output(raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Separate the retrieval telemetry from the passages the model was given."""
    try:
        payload = json.loads(raw, strict=False)
    except (json.JSONDecodeError, TypeError):
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    return payload, payload.get("references") or []


def translate(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield zero or more trace events for one Responses stream event."""
    kind = event.get("type")

    if kind == "response.created":
        yield _event("run.start", response_id=(event.get("response") or {}).get("id"))

    elif kind == "response.output_item.added":
        item = event.get("item") or {}
        if item.get("type") == "function_call":
            yield _event("tool.call", name=item.get("name"), call_id=item.get("call_id"))

    elif kind == "response.function_call_arguments.done":
        # The exact query the model decided to send to the knowledge base.
        query = None
        try:
            query = json.loads(event.get("arguments") or "{}").get("query")
        except json.JSONDecodeError:
            pass
        yield _event(
            "tool.args",
            name=event.get("name"),
            query=query,
            raw=event.get("arguments"),
        )

    elif kind == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") != "function_call_output":
            return
        payload, references = _split_tool_output(item.get("output") or "")

        if payload.get("error"):
            yield _event("tool.error", message=payload["error"], detail=payload.get("detail"))
            return

        activity = payload.get("retrieval_activity") or {}

        # The knowledge base re-plans after seeing first-round results, so there is
        # usually more than one planning step.
        for index, planning in enumerate(activity.get("planning") or []):
            yield _event(
                "retrieval.plan",
                index=index,
                step_id=planning.get("id"),
                model=planning.get("model"),
                input_tokens=planning.get("input_tokens"),
                output_tokens=planning.get("output_tokens"),
                elapsed_ms=planning.get("elapsed_ms"),
            )

        for index, subquery in enumerate(activity.get("subqueries") or []):
            yield _event(
                "retrieval.query",
                index=index,
                step_id=subquery.get("id"),
                search=subquery.get("search"),
                knowledge_source=subquery.get("knowledge_source"),
                source_kind=subquery.get("kind"),
                count=subquery.get("count"),
                elapsed_ms=subquery.get("elapsed_ms"),
            )

        reasoning = activity.get("reasoning") or {}
        if reasoning:
            yield _event(
                "retrieval.reasoning",
                reasoning_tokens=reasoning.get("reasoning_tokens"),
                retrieval_effort=reasoning.get("retrieval_effort"),
                logical_effort=reasoning.get("logical_effort"),
            )

        yield _event(
            "retrieval.refs",
            references=references,
            passage_count=len(payload.get("passages") or []),
            reference_count=activity.get("reference_count"),
            total_elapsed_ms=activity.get("total_elapsed_ms"),
        )

    elif kind == "response.output_text.delta":
        yield _event("answer.delta", text=event.get("delta") or "")

    elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        yield _event("model.reasoning", text=event.get("delta") or "")

    elif kind == "response.completed":
        response = event.get("response") or {}
        yield _event(
            "run.done",
            response_id=response.get("id"),
            usage=response.get("usage") or {},
        )

    elif kind in ("response.failed", "response.incomplete", "error"):
        response = event.get("response") or {}
        error = response.get("error") or event.get("error") or {}
        yield _event(
            "run.error",
            message=error.get("message") or "The run did not complete.",
            code=error.get("code"),
        )

    elif kind in (
        # Structural events with no standalone meaning in the timeline.
        "response.in_progress",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.function_call_arguments.delta",
    ):
        return

    else:
        yield _event("raw", event_type=kind, payload=event)
