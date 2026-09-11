import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import type { Turn } from "../types";

const SUGGESTIONS = [
  "What is the nose gear jacking procedure for the A320neo?",
  "What is the maximum load on the NLG jacking point?",
  "How many wheels does the main landing gear have?",
  "What are the towing limitations for the A320?",
];

function Answer({ turn }: { turn: Turn }) {
  if (turn.error) {
    return <div className="error-box">{turn.error}</div>;
  }
  if (!turn.answer) {
    return (
      <div className="thinking">
        <span className="blink" />
        {turn.trace.some((e) => e.type === "tool.call" || e.type === "tool.args")
          ? "Searching the knowledge base — this can take up to a minute…"
          : "Thinking…"}
      </div>
    );
  }
  return (
    <>
      <div className="a">
        <Markdown>{turn.answer}</Markdown>
      </div>
      {turn.references.length > 0 && (
        <div className="cites">
          {turn.references.map((reference) => (
            <a
              key={reference.ref_id}
              className="cite"
              href={reference.citation_url ?? "#"}
              target="_blank"
              rel="noreferrer"
              title={reference.citation_url ?? undefined}
            >
              <b>[{reference.ref_id}]</b>
              {reference.doc_name ?? "source"}
            </a>
          ))}
        </div>
      )}
      {turn.usage && (
        <div className="meta">
          <span>in {turn.usage.input_tokens?.toLocaleString() ?? "—"}</span>
          <span>out {turn.usage.output_tokens?.toLocaleString() ?? "—"}</span>
          {turn.endedAt && <span>{((turn.endedAt - turn.startedAt) / 1000).toFixed(1)}s</span>}
        </div>
      )}
    </>
  );
}

export function ChatPanel({
  turns,
  busy,
  onSend,
  onSelect,
  selectedId,
}: {
  turns: Turn[];
  busy: boolean;
  onSend: (question: string) => void;
  onSelect: (id: string) => void;
  selectedId: string | null;
}) {
  const [draft, setDraft] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns.length, turns[turns.length - 1]?.answer]);

  const submit = () => {
    const question = draft.trim();
    if (!question || busy) return;
    onSend(question);
    setDraft("");
  };

  return (
    <main className="chat">
      <div className="messages">
        {turns.length === 0 && (
          <div className="empty">
            <h2>Airbus maintenance assistant</h2>
            <p>
              A Foundry hosted agent grounded in the <code>airplane-manufacturing</code>{" "}
              Foundry IQ knowledge base. Every answer is retrieved and cited — the panel on
              the right shows exactly how.
            </p>
          </div>
        )}

        {turns.map((turn) => (
          <div
            className="turn"
            key={turn.id}
            onClick={() => onSelect(turn.id)}
            style={{ opacity: selectedId && selectedId !== turn.id ? 0.62 : 1, cursor: "pointer" }}
          >
            <div className="q">{turn.question}</div>
            <Answer turn={turn} />
          </div>
        ))}
        <div ref={bottom} />
      </div>

      {turns.length === 0 && (
        <div className="suggestions">
          {SUGGESTIONS.map((suggestion) => (
            <button key={suggestion} onClick={() => onSend(suggestion)} disabled={busy}>
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <div className="composer">
        <textarea
          value={draft}
          placeholder="Ask about Airbus manuals, parts or procedures…"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          rows={1}
        />
        <button className="primary" onClick={submit} disabled={busy || !draft.trim()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </main>
  );
}
