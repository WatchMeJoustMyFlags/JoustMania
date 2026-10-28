/**
 * Jaeger HTTP query client (#2059): the dashboard's agent and experiment panels
 * read spans, never logs.
 *
 * API (#2283): the query API v3 through envoy (Jaeger runs with base_path
 * /jaeger), GET /jaeger/api/v3/traces?query.serviceName=agent
 * &query.operationName=<op>&query.attributes=<JSON>&query.searchDepth=<n>
 * &query.startTimeMin=<RFC 3339>&query.startTimeMax=<RFC 3339>. Jaeger 2.21
 * removed the legacy /api/traces search. Search returns WHOLE traces,
 * `query.searchDepth` counts traces, attribute filters are AND-only string
 * matches, and a span shows up only once it has ENDED. The answer is OTLP JSON;
 * fromOtlp() turns it into the span shape the folds below and in agent.ts /
 * experiments.ts read (the field names of the legacy API). Every fold happens
 * client-side.
 *
 * CLOCK SKEW: the stage Pi may have no NTP, so the agent + Jaeger clock (same
 * host) can be hours off the browser clock. A query NEVER carries a browser-clock
 * time. v3 requires both window bounds, so the upper bound is a fixed far-future
 * date (SEARCH_TIME_MAX, Jaeger's own now in effect), the lower bound on a cold
 * start is the Unix epoch (the whole store, bounded by the search depth, newest
 * traces first), and later a start derived only from span timestamps already
 * seen (`incrementalStartMs`).
 */

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

/** The agent's OTel service name in Jaeger. */
export const AGENT_SERVICE = "agent";

/*
 * The span shape every fold reads: the field names of Jaeger's legacy query API,
 * built from the v3 OTLP answer by fromOtlp() (#2283).
 */

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

/** A search answer after fromOtlp(): the matching traces. */
export interface JaegerTracesResponse {
  data?: JaegerTrace[] | null;
}

/** An OTLP JSON attribute value (protobuf JSON: 64-bit integers are strings). */
export interface OtlpAnyValue {
  stringValue?: string;
  boolValue?: boolean;
  intValue?: string | number;
  doubleValue?: number;
  bytesValue?: string;
  arrayValue?: { values?: OtlpAnyValue[] };
  kvlistValue?: { values?: OtlpKeyValue[] };
}

export interface OtlpKeyValue {
  key: string;
  value?: OtlpAnyValue;
}

export interface OtlpSpanEvent {
  /** Nanoseconds since epoch, as a decimal string. */
  timeUnixNano?: string;
  name?: string;
  attributes?: OtlpKeyValue[];
}

export interface OtlpSpanLink {
  traceId?: string;
  spanId?: string;
}

export interface OtlpSpan {
  traceId: string;
  spanId: string;
  /** Absent or "" for a root span. */
  parentSpanId?: string;
  name: string;
  startTimeUnixNano?: string;
  endTimeUnixNano?: string;
  attributes?: OtlpKeyValue[];
  events?: OtlpSpanEvent[];
  links?: OtlpSpanLink[];
}

export interface OtlpResourceSpans {
  resource?: { attributes?: OtlpKeyValue[] };
  scopeSpans?: { spans?: OtlpSpan[] | null }[] | null;
}

/**
 * A v3 search / get-trace answer: `result` on success, `error` on a failure
 * (`404` "No traces found" when nothing matched).
 */
export interface OtlpTracesResponse {
  result?: { resourceSpans?: OtlpResourceSpans[] | null } | null;
  error?: { httpCode?: number; message?: string } | null;
}

/** The plain value of an OTLP attribute: number, boolean or string (arrays and maps as JSON). */
export function otlpValue(v: OtlpAnyValue | undefined): unknown {
  if (!v) return "";
  if (v.stringValue !== undefined) return v.stringValue;
  if (v.boolValue !== undefined) return v.boolValue;
  if (v.intValue !== undefined) return Number(v.intValue);
  if (v.doubleValue !== undefined) return v.doubleValue;
  if (v.bytesValue !== undefined) return v.bytesValue;
  if (v.arrayValue !== undefined) return JSON.stringify((v.arrayValue.values ?? []).map(otlpValue));
  if (v.kvlistValue !== undefined) {
    const obj: Record<string, unknown> = {};
    for (const kv of v.kvlistValue.values ?? []) obj[kv.key] = otlpValue(kv.value);
    return JSON.stringify(obj);
  }
  return "";
}

function otlpTags(attrs: OtlpKeyValue[] | undefined): JaegerTag[] {
  return (attrs ?? []).map((a) => ({ key: a.key, value: otlpValue(a.value) }));
}

/** OTLP nanoseconds (decimal string) to microseconds; exact for any 2001-2286 time. */
export function nanosToMicros(ns: string | number | undefined): number {
  if (ns === undefined || ns === "") return 0;
  if (typeof ns === "number") return Math.floor(ns / 1000);
  return ns.length > 3 ? Number(ns.slice(0, -3)) : 0;
}

/**
 * Turn a v3 answer into the span shape the folds read (#2283): spans grouped by
 * `traceId` (v3 lists every matching trace's resource spans in one list),
 * `name` as operationName, `parentSpanId` as the CHILD_OF reference, links as
 * FOLLOWS_FROM, `attributes` as tags, span events as logs with an `event` field,
 * times in microseconds. Pure.
 */
export function fromOtlp(body: OtlpTracesResponse | null | undefined): JaegerTracesResponse {
  const traces = new Map<string, JaegerTrace>();
  for (const rs of body?.result?.resourceSpans ?? []) {
    for (const ss of rs.scopeSpans ?? []) {
      for (const s of ss.spans ?? []) {
        const references: JaegerReference[] = [];
        // A parent that is also a link is a follows-from edge only (legacy shape):
        // the span stays a root of its own trace.
        const linked = new Set((s.links ?? []).map((l) => l.spanId));
        if (s.parentSpanId && !linked.has(s.parentSpanId)) references.push({ refType: "CHILD_OF", traceID: s.traceId, spanID: s.parentSpanId });
        for (const l of s.links ?? []) {
          if (l.spanId) references.push({ refType: "FOLLOWS_FROM", traceID: l.traceId ?? "", spanID: l.spanId });
        }
        const startTime = nanosToMicros(s.startTimeUnixNano);
        const endTime = nanosToMicros(s.endTimeUnixNano);
        const span: JaegerSpan = {
          traceID: s.traceId,
          spanID: s.spanId,
          operationName: s.name,
          references,
          startTime,
          duration: Math.max(0, endTime - startTime),
          tags: otlpTags(s.attributes),
          logs: (s.events ?? []).map((e) => ({
            timestamp: nanosToMicros(e.timeUnixNano),
            fields: [{ key: "event", value: e.name ?? "" }, ...otlpTags(e.attributes)],
          })),
        };
        let trace = traces.get(s.traceId);
        if (!trace) {
          trace = { traceID: s.traceId, spans: [] };
          traces.set(s.traceId, trace);
        }
        trace.spans.push(span);
      }
    }
  }
  return { data: [...traces.values()] };
}

/** True for v3's "nothing matched" answer (404 with `No traces found`), which is data. */
export function isNoTracesAnswer(status: number, body: OtlpTracesResponse | null): boolean {
  return status === 404 && body?.error?.httpCode === 404 && /no traces found/i.test(body.error.message ?? "");
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

/**
 * Span start in epoch µs (agent clock), Jaeger's own resolution. Ordering uses
 * it, not spanStartMs: two snapshots of one entity often fall in the same ms.
 */
export function spanStartUs(span: JaegerSpan): number {
  return span.startTime;
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

/** v3 search upper bound: fixed and far in the future, so no query carries a browser-clock time. */
export const SEARCH_TIME_MAX = "2100-01-01T00:00:00Z";

/** v3 search lower bound on a cold start (v3 requires one): the Unix epoch, i.e. the whole store. */
export const COLD_START_TIME_MIN = "1970-01-01T00:00:00Z";

export interface SearchSpec {
  operation: string;
  /** Max traces (`query.searchDepth`). */
  limit: number;
  /** Agent-clock ms (from span timestamps only), or null on a cold start (COLD_START_TIME_MIN). */
  startMs: number | null;
  tags?: Record<string, string>;
}

/** Query string of a v3 Jaeger search. Never contains a browser-clock time. */
export function searchParams(spec: SearchSpec): URLSearchParams {
  const params = new URLSearchParams({
    "query.serviceName": AGENT_SERVICE,
    "query.operationName": spec.operation,
    "query.searchDepth": String(spec.limit),
    "query.startTimeMin":
      spec.startMs === null ? COLD_START_TIME_MIN : new Date(Math.floor(spec.startMs)).toISOString(),
    "query.startTimeMax": SEARCH_TIME_MAX,
  });
  if (spec.tags && Object.keys(spec.tags).length > 0) params.set("query.attributes", JSON.stringify(spec.tags));
  return params;
}

/** A Jaeger search answer plus its size (characters of the JSON body, #2287). */
export interface SizedSearch {
  body: JaegerTracesResponse;
  bytes: number;
}

/**
 * Run one Jaeger search and report the size of its answer (#2287): Jaeger
 * returns whole traces, so the size is what the poll cost Jaeger to decode and
 * send, and the decide poll paces itself on it. The size is that of the v3
 * (OTLP) body, about 10% below the legacy one. Throws JaegerUnavailableError
 * when Jaeger cannot answer (fetch rejected, aborted by the poller's timeout,
 * non-2xx such as envoy's 503, or a v3 `error` other than "No traces found").
 * No matching trace (v3 answers 404 `No traces found`) is data, not an error:
 * an empty response.
 */
export async function searchTracesSized(spec: SearchSpec, signal?: AbortSignal): Promise<SizedSearch> {
  const url = `${BASE_URL}/jaeger/api/v3/traces?${searchParams(spec).toString()}`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    throw new JaegerUnavailableError(`Jaeger unreachable: ${String(error)}`);
  }
  if (!response.ok && response.status !== 404) {
    throw new JaegerUnavailableError(`Jaeger query failed: ${response.status} ${response.statusText}`);
  }
  let text: string;
  let body: OtlpTracesResponse | null;
  try {
    text = await response.text();
    body = JSON.parse(text) as OtlpTracesResponse | null;
  } catch (error) {
    if (response.status === 404) throw new JaegerUnavailableError(`Jaeger query failed: 404 ${response.statusText}`);
    throw new JaegerUnavailableError(`Jaeger answered no JSON: ${String(error)}`);
  }
  if (isNoTracesAnswer(response.status, body)) return { body: { data: [] }, bytes: text.length };
  if (response.status === 404 || body?.error) {
    const err = body?.error;
    throw new JaegerUnavailableError(`Jaeger error: ${err?.message ?? err?.httpCode ?? response.status}`);
  }
  return { body: fromOtlp(body), bytes: text.length };
}

/** Run one Jaeger search (see searchTracesSized). */
export async function searchTraces(spec: SearchSpec, signal?: AbortSignal): Promise<JaegerTracesResponse> {
  return (await searchTracesSized(spec, signal)).body;
}
