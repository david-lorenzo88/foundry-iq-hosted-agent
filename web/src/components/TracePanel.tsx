import { Elapsed } from "./Elapsed";
import type { Reference, TraceEvent, Turn } from "../types";

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const fmt = (value: number | null): string =>
  value === null ? "—" : value.toLocaleString();

/** Longest step in the run, so the duration bars share one scale. */
function slowestStep(trace: TraceEvent[]): number {
  return trace.reduce((max, event) => Math.max(max, num(event.data.elapsed_ms) ?? 0), 1);
}

function Raw({ data }: { data: unknown }) {
  return (
    <details className="raw">
      <summary>raw</summary>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  );
}

function Card({ event, scale }: { event: TraceEvent; scale: number }) {
  const { type, data } = event;
  const ms = num(data.elapsed_ms);
  const bar = ms === null ? null : Math.max(2, (ms / scale) * 100);

  switch (type) {
    case "tool.args":
      return (
        <div className="card agent">
          <div className="card-top">
            <span className="card-title">Agent called {String(data.name)}</span>
          </div>
          <div className="query">{String(data.query ?? data.raw ?? "")}</div>
          <Raw data={data} />
        </div>
      );

    case "retrieval.plan":
      return (
        <div className="card kb">
          <div className="card-top">
            <span className="card-title">
              Query planning {num(data.index) !== null ? `#${(num(data.index) ?? 0) + 1}` : ""}
            </span>
            <span className="card-time">{fmt(ms)} ms</span>
          </div>
          <div className="stats">
            <span>{String(data.model ?? "—")}</span>
            <span>in {fmt(num(data.input_tokens))}</span>
            <span>out {fmt(num(data.output_tokens))}</span>
          </div>
          {bar !== null && <div className="bar"><i style={{ width: `${bar}%` }} /></div>}
          <Raw data={data} />
        </div>
      );

    case "retrieval.query":
      return (
        <div className="card kb">
          <div className="card-top">
            <span className="card-title">Subquery {(num(data.index) ?? 0) + 1}</span>
            <span className="card-time">{fmt(ms)} ms</span>
          </div>
          <div className="query">{String(data.search ?? "(no search text)")}</div>
          <div className="stats">
            <span>{String(data.knowledge_source ?? "—")}</span>
            <span>{String(data.source_kind ?? "—")}</span>
            <span>{fmt(num(data.count))} hits</span>
          </div>
          {bar !== null && <div className="bar"><i style={{ width: `${bar}%` }} /></div>}
          <Raw data={data} />
        </div>
      );

    case "retrieval.reasoning":
      return (
        <div className="card kb">
          <div className="card-top">
            <span className="card-title">Agentic reasoning</span>
          </div>
          <div className="stats">
            <span>{fmt(num(data.reasoning_tokens))} reasoning tokens</span>
            <span>retrieval: {String(data.retrieval_effort ?? "—")}</span>
            <span>logical: {String(data.logical_effort ?? "—")}</span>
          </div>
          <Raw data={data} />
        </div>
      );

    case "retrieval.refs": {
      const references = (data.references as Reference[]) ?? [];
      const best = references.reduce((max, r) => Math.max(max, r.score ?? 0), 1);
      return (
        <div className="card kb">
          <div className="card-top">
            <span className="card-title">Retrieved documents</span>
            <span className="card-time">{fmt(num(data.total_elapsed_ms))} ms</span>
          </div>
          <div className="stats">
            <span>{fmt(num(data.passage_count))} passages to model</span>
            <span>{fmt(num(data.reference_count))} matched</span>
          </div>
          <div className="docs">
            {references.map((reference) => (
              <div className="doc" key={reference.ref_id}>
                <span className="rid">[{reference.ref_id}]</span>
                <span className="name" title={reference.doc_name ?? ""}>
                  {reference.doc_name ?? "(unnamed)"}
                </span>
                <span className="score">
                  {reference.score !== null ? reference.score.toFixed(2) : "—"}
                </span>
                <div className="bar" style={{ width: 42, marginTop: 0 }}>
                  <i style={{ width: `${((reference.score ?? 0) / best) * 100}%` }} />
                </div>
              </div>
            ))}
          </div>
          <Raw data={data} />
        </div>
      );
    }

    case "run.done": {
      const usage = (data.usage ?? {}) as Record<string, number>;
      return (
        <div className="card done">
          <div className="card-top">
            <span className="card-title">Run complete</span>
          </div>
          <div className="stats">
            <span>in {fmt(num(usage.input_tokens))}</span>
            <span>out {fmt(num(usage.output_tokens))}</span>
            <span>total {fmt(num(usage.total_tokens))}</span>
          </div>
          <Raw data={data} />
        </div>
      );
    }

    case "run.error":
    case "tool.error":
      return (
        <div className="card bad">
          <div className="card-top">
            <span className="card-title">Error</span>
          </div>
          <div className="query">{String(data.message ?? "")}</div>
          {data.detail ? <Raw data={data.detail} /> : null}
        </div>
      );

    case "model.reasoning":
      return (
        <div className="card agent">
          <div className="card-top">
            <span className="card-title">Model reasoning</span>
          </div>
          <div className="query">{String(data.text ?? "")}</div>
        </div>
      );

    case "run.start":
      return (
        <div className="card agent">
          <div className="card-top">
            <span className="card-title">Run started</span>
          </div>
          <div className="stats">
            <span>{String(data.response_id ?? "")}</span>
          </div>
        </div>
      );

    case "tool.call":
      // Superseded by tool.args, which carries the actual query.
      return null;

    default:
      return (
        <div className="card">
          <div className="card-top">
            <span className="card-title">{String(data.event_type ?? type)}</span>
          </div>
          <Raw data={data} />
        </div>
      );
  }
}

/** Which phase heading a card sits under. */
function phaseOf(type: string): "Agent" | "Foundry IQ knowledge base" | "Result" {
  if (type.startsWith("retrieval.")) return "Foundry IQ knowledge base";
  if (type === "run.done") return "Result";
  return "Agent";
}

export function TracePanel({ turn }: { turn: Turn | null }) {
  if (!turn || turn.trace.length === 0) {
    return (
      <aside className="trace">
        <div className="trace-head">
          <h2>Run trace</h2>
        </div>
        <div className="trace-body">
          <p className="trace-empty">
            Ask a question to see how the agent plans its search, what the knowledge base
            actually queried, and which documents came back.
          </p>
        </div>
      </aside>
    );
  }

  const scale = slowestStep(turn.trace);
  const elapsed = turn.endedAt ? ((turn.endedAt - turn.startedAt) / 1000).toFixed(1) : null;

  // Agentic retrieval regularly runs for 30-60s with nothing on the wire. Derive the
  // in-flight step from what has arrived rather than from the last event, because some
  // events (tool.call) render nothing and would otherwise leave a silent gap.
  const pending = (() => {
    if (turn.status !== "streaming") return null;

    const at = (type: string) => turn.trace.find((e) => e.type === type);
    const called = at("tool.call") ?? at("tool.args");
    const returned = turn.trace.some((e) => e.type.startsWith("retrieval."));

    if (called && !returned) {
      return {
        phase: "Foundry IQ knowledge base" as const,
        title: "Querying knowledge base",
        note: "planning, decomposing and searching",
        since: called.ts * 1000,
      };
    }
    if (!called) {
      return {
        phase: "Agent" as const,
        title: "Model deciding",
        note: "choosing whether to search",
        since: turn.startedAt,
      };
    }
    if (returned && !turn.answer) {
      return {
        phase: "Result" as const,
        title: "Composing answer",
        note: "grounding the response in the retrieved passages",
        since: (at("retrieval.refs")?.ts ?? turn.startedAt / 1000) * 1000,
      };
    }
    return null;
  })();

  let lastPhase = "";

  return (
    <aside className="trace">
      <div className="trace-head">
        <h2>Run trace</h2>
        <span className="pill">
          <span className={`dot ${turn.status === "error" ? "err" : turn.status === "done" ? "ok" : ""}`} />
          {turn.status}
        </span>
        {elapsed && <span className="card-time">{elapsed}s</span>}
      </div>
      <div className="trace-body">
        {turn.trace.map((event, index) => {
          const phase = phaseOf(event.type);
          const heading = phase !== lastPhase ? phase : null;
          lastPhase = phase;
          return (
            <div key={`${event.type}-${index}`}>
              {heading && <div className="phase">{heading}</div>}
              <Card event={event} scale={scale} />
            </div>
          );
        })}
        {pending && (
          <>
            {pending.phase !== lastPhase && <div className="phase">{pending.phase}</div>}
            <div className={`card ${pending.phase === "Foundry IQ knowledge base" ? "kb" : "agent"}`}>
              <div className="card-top">
                <span className="card-title">{pending.title}</span>
                <Elapsed since={pending.since} />
              </div>
              <div className="stats">
                <span className="blink" style={{ height: 10 }} />
                <span>{pending.note}</span>
              </div>
            </div>
          </>
        )}
      </div>
    </aside>
  );
}
