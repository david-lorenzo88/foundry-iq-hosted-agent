import { useCallback, useRef, useState } from "react";
import type { Health, Reference, TraceEvent, Turn, Usage } from "./types";

export async function fetchHealth(): Promise<Health> {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error(`Health check failed: ${response.status}`);
  return response.json();
}

function newTurn(question: string): Turn {
  return {
    id: crypto.randomUUID(),
    question,
    answer: "",
    trace: [],
    references: [],
    usage: null,
    responseId: null,
    error: null,
    status: "streaming",
    startedAt: Date.now(),
    endedAt: null,
  };
}

/** Fold one trace event into the turn it belongs to. */
function reduceTurn(turn: Turn, event: TraceEvent): Turn {
  switch (event.type) {
    case "answer.delta":
      return { ...turn, answer: turn.answer + String(event.data.text ?? "") };
    case "retrieval.refs":
      return {
        ...turn,
        references: (event.data.references as Reference[]) ?? [],
        trace: [...turn.trace, event],
      };
    case "run.done":
      return {
        ...turn,
        usage: (event.data.usage as Usage) ?? null,
        responseId: (event.data.response_id as string) ?? turn.responseId,
        status: "done",
        endedAt: Date.now(),
        trace: [...turn.trace, event],
      };
    case "run.start":
      return {
        ...turn,
        responseId: (event.data.response_id as string) ?? null,
        trace: [...turn.trace, event],
      };
    case "run.error":
    case "tool.error":
      return {
        ...turn,
        error: String(event.data.message ?? "Something went wrong."),
        status: "error",
        endedAt: Date.now(),
        trace: [...turn.trace, event],
      };
    default:
      return { ...turn, trace: [...turn.trace, event] };
  }
}

export function useAgentStream() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  // The response id of the last completed turn, used to thread the conversation.
  const previousResponseId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const send = useCallback(async (question: string) => {
    const turn = newTurn(question);
    setTurns((current) => [...current, turn]);
    setBusy(true);

    const controller = new AbortController();
    abort.current = controller;

    // Mutated locally then published, so a burst of deltas is one state update per
    // chunk rather than one per event.
    let working = turn;
    const publish = () =>
      setTurns((current) => current.map((t) => (t.id === working.id ? working : t)));

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: question,
          previous_response_id: previousResponseId.current,
        }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(`Agent request failed: ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line; keep any partial tail.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let event: TraceEvent;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          working = reduceTurn(working, event);
        }
        publish();
      }

      if (working.status === "streaming") {
        working = { ...working, status: "done", endedAt: Date.now() };
      }
      if (working.responseId) previousResponseId.current = working.responseId;
      publish();
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        working = {
          ...working,
          error: (error as Error).message,
          status: "error",
          endedAt: Date.now(),
        };
      }
      publish();
    } finally {
      setBusy(false);
      abort.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    abort.current?.abort();
    previousResponseId.current = null;
    setTurns([]);
  }, []);

  return { turns, busy, send, reset };
}
