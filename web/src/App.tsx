import { useEffect, useMemo, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { TracePanel } from "./components/TracePanel";
import { fetchHealth, useAgentStream } from "./api";
import type { Health } from "./types";

export default function App() {
  const { turns, busy, send, reset } = useAgentStream();
  const [health, setHealth] = useState<Health | null>(null);
  const [pinnedId, setPinnedId] = useState<string | null>(null);

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  // The trace panel follows the newest run unless the user pins an earlier one.
  const shown = useMemo(() => {
    if (pinnedId) return turns.find((turn) => turn.id === pinnedId) ?? null;
    return turns[turns.length - 1] ?? null;
  }, [turns, pinnedId]);

  return (
    <div className="app">
      <header className="header">
        <h1>Airbus Maintenance Agent</h1>
        <span className="sub">Foundry hosted agent · Foundry IQ</span>
        <div className="spacer" />
        {health && (
          <>
            <span className="pill">
              <span className={`dot ${health.reachable ? "ok" : "err"}`} />
              {health.target}
              {health.version ? ` v${health.version}` : ""}
            </span>
            <span className="pill">{health.model}</span>
          </>
        )}
        <button onClick={() => { reset(); setPinnedId(null); }} disabled={busy || turns.length === 0}>
          New conversation
        </button>
      </header>

      <div className="split">
        <ChatPanel
          turns={turns}
          busy={busy}
          onSend={send}
          onSelect={(id) => setPinnedId((current) => (current === id ? null : id))}
          selectedId={pinnedId}
        />
        <TracePanel turn={shown} />
      </div>
    </div>
  );
}
