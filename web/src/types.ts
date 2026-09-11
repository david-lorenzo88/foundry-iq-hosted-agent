/** The trace vocabulary the backend emits. The raw OpenAI Responses shapes stop at
 *  the backend, so this file is the whole contract between the two halves. */

export type TraceType =
  | "run.start"
  | "tool.call"
  | "tool.args"
  | "tool.error"
  | "retrieval.plan"
  | "retrieval.query"
  | "retrieval.reasoning"
  | "retrieval.refs"
  | "model.reasoning"
  | "answer.delta"
  | "run.done"
  | "run.error"
  | "raw";

export interface TraceEvent {
  type: TraceType;
  ts: number;
  data: Record<string, unknown>;
}

export interface Reference {
  ref_id: string;
  type: string | null;
  doc_name: string | null;
  score: number | null;
  activity_source: number | null;
  citation_url: string | null;
}

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
}

export interface Turn {
  id: string;
  question: string;
  answer: string;
  /** Everything except answer deltas, in arrival order. */
  trace: TraceEvent[];
  references: Reference[];
  usage: Usage | null;
  responseId: string | null;
  error: string | null;
  status: "streaming" | "done" | "error";
  startedAt: number;
  endedAt: number | null;
}

export interface Health {
  target: "local" | "foundry";
  agent: string;
  model: string;
  endpoint: string;
  reachable: boolean;
  version?: string;
  state?: string;
  error?: string;
}
