/**
 * Jaeger HTTP query client (#2059): the dashboard's agent and experiment panels
 * read spans, never logs.
 *
 * API: GET /jaeger/api/traces?service=agent&operation=<op>&tags=<JSON>&limit=<n>&start=<µs>
 * through envoy (Jaeger runs with base_path /jaeger). Search returns WHOLE traces
 * and `limit` counts traces, tag filters are AND-only string matches, and a span
 * shows up only once it has ENDED. Every fold happens here, client-side.
 *
 * CLOCK SKEW: the stage Pi may have no NTP, so the agent + Jaeger clock (same
 * host) can be hours off the browser clock. A query NEVER carries a browser-clock
 * time: no `end` (Jaeger uses its own now), no `start` on a cold start (Jaeger's
 * own default window, bounded by `limit`, newest traces first), and later a
 * `start` derived only from span timestamps already seen (`incrementalStartMs`).
 */

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

/** The agent's OTel service name in Jaeger. */
export const AGENT_SERVICE = "agent";

export interface JaegerTag {
  key: string;
  type?: string;
  value: unknown;
}

export interface JaegerReference {
  refType: string;
  traceID: string;
  spanID: string;
}

/** A span event, as Jaeger renders it (the event name is the `event` field). */
export interface JaegerLog {
  timestamp: number;
  fields?: JaegerTag[];
}

export interface JaegerSpan {
  traceID: string;
  spanID: string;
  operationName: string;
  references?: JaegerReference[];
  /** Microseconds since epoch (agent clock). */
  startTime: number;
  /** Microseconds. */
  duration: number;
  tags?: JaegerTag[];
  logs?: JaegerLog[];
}

export interface JaegerTrace {
  traceID: string;
  spans: JaegerSpan[];
}

export interface JaegerTracesResponse {
  data?: JaegerTrace[] | null;
  errors?: { code?: number; msg?: string }[] | null;
}

/** Jaeger could not answer: transport error, timeout, non-2xx or a Jaeger error. */
export class JaegerUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "JaegerUnavailableError";
  }
}

export function tagMap(tags: JaegerTag[] | undefined): Map<string, unknown> {
  const m = new Map<string, unknown>();
  for (const t of tags ?? []) m.set(t.key, t.value);
  return m;
}

export function spanTags(span: JaegerSpan): Map<string, unknown> {
  return tagMap(span.tags);
}

export function tagString(tags: Map<string, unknown>, key: string): string {
  const v = tags.get(key);
  if (v === undefined || v === null) return "";
  return String(v);
}

export function tagNumber(tags: Map<string, unknown>, key: string): number | null {
  const v = tags.get(key);
  if (v === undefined || v === null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function tagBool(tags: Map<string, unknown>, key: string): boolean {
  const v = tags.get(key);
  return v === true || v === "true";
}

/** true / false when the tag is a boolean, null when it is absent. */
export function tagBoolOrNull(tags: Map<string, unknown>, key: string): boolean | null {
  const v = tags.get(key);
  if (v === true || v === "true") return true;
  if (v === false || v === "false") return false;
  return null;
}

/** The CHILD_OF parent span id ("" for a root). */
export function parentSpanId(span: JaegerSpan): string {
  const ref = (span.references ?? []).find((r) => r.refType === "CHILD_OF");
  return ref?.spanID ?? "";
}

/** Span start in epoch ms (agent clock). */
export function spanStartMs(span: JaegerSpan): number {
  return Math.floor(span.startTime / 1000);
}

/** The span events named `name`, each as a tag map. */
export function spanEvents(span: JaegerSpan, name: string): Map<string, unknown>[] {
  const out: Map<string, unknown>[] = [];
  for (const log of span.logs ?? []) {
    const fields = tagMap(log.fields);
    if (fields.get("event") === name) out.push(fields);
  }
  return out;
}

/** Every span of a response with operation `operation`, deduplicated by trace/span id. */
export function spansNamed(body: JaegerTracesResponse, operation: string): JaegerSpan[] {
  const out: JaegerSpan[] = [];
  const seen = new Set<string>();
  for (const trace of body.data ?? []) {
    for (const s of trace.spans ?? []) {
      if (s.operationName !== operation) continue;
      const key = `${s.traceID}/${s.spanID}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(s);
    }
  }
  return out;
}

/**
 * Start of the next incremental search (epoch ms, AGENT clock): `overlapMs`
 * behind the newest span already held, because a span that started earlier can
 * end (and be exported) later. null on a cold start: send no `start`.
 */
export function incrementalStartMs(newestSeenMs: number | null, overlapMs: number): number | null {
  return newestSeenMs === null ? null : newestSeenMs - overlapMs;
}

export interface SearchSpec {
  operation: string;
  limit: number;
  /** Agent-clock ms (from span timestamps only), or null for Jaeger's default window. */
  startMs: number | null;
  tags?: Record<string, string>;
}

/** Query string of a Jaeger search. Never contains a browser-clock time. */
export function searchParams(spec: SearchSpec): URLSearchParams {
  const params = new URLSearchParams({
    service: AGENT_SERVICE,
    operation: spec.operation,
    limit: String(spec.limit),
  });
  if (spec.tags && Object.keys(spec.tags).length > 0) params.set("tags", JSON.stringify(spec.tags));
  if (spec.startMs !== null) params.set("start", String(Math.floor(spec.startMs) * 1000));
  return params;
}

/**
 * Run one Jaeger search. Throws JaegerUnavailableError when Jaeger cannot answer
 * (fetch rejected, aborted by the poller's timeout, non-2xx such as envoy's 503,
 * or a non-empty `errors` array). A 200 with no traces is data, not an error.
 */
export async function searchTraces(spec: SearchSpec, signal?: AbortSignal): Promise<JaegerTracesResponse> {
  const url = `${BASE_URL}/jaeger/api/traces?${searchParams(spec).toString()}`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    throw new JaegerUnavailableError(`Jaeger unreachable: ${String(error)}`);
  }
  if (!response.ok) {
    throw new JaegerUnavailableError(`Jaeger query failed: ${response.status} ${response.statusText}`);
  }
  let body: JaegerTracesResponse;
  try {
    body = (await response.json()) as JaegerTracesResponse;
  } catch (error) {
    throw new JaegerUnavailableError(`Jaeger answered no JSON: ${String(error)}`);
  }
  if (body.errors && body.errors.length > 0) {
    throw new JaegerUnavailableError(`Jaeger error: ${body.errors.map((e) => e.msg ?? e.code).join("; ")}`);
  }
  return body;
}
