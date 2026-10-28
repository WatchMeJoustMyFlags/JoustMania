/**
 * Test helper (#2283): turn a fixture in the legacy span shape (the folds' input)
 * into the Jaeger v3 OTLP JSON answer, so tests that stub `fetch` serve what
 * Jaeger serves. The inverse of fromOtlp() for the fields the fixtures use.
 */
import type { JaegerTag, JaegerTracesResponse, OtlpAnyValue, OtlpKeyValue, OtlpTracesResponse } from "../jaeger.js";

function anyValue(v: unknown): OtlpAnyValue {
  if (typeof v === "boolean") return { boolValue: v };
  if (typeof v === "number") return Number.isInteger(v) ? { intValue: String(v) } : { doubleValue: v };
  return { stringValue: v === undefined || v === null ? "" : String(v) };
}

function attributes(tags: JaegerTag[] | undefined): OtlpKeyValue[] {
  return (tags ?? []).map((t) => ({ key: t.key, value: anyValue(t.value) }));
}

const nanos = (us: number): string => (BigInt(Math.round(us)) * 1000n).toString();

export function toOtlp(body: unknown): OtlpTracesResponse {
  const legacy = body as JaegerTracesResponse;
  return {
    result: {
      resourceSpans: (legacy.data ?? []).map((trace) => ({
        resource: { attributes: [{ key: "service.name", value: { stringValue: "agent" } }] },
        scopeSpans: [
          {
            spans: trace.spans.map((s) => {
              const parent = (s.references ?? []).find((r) => r.refType === "CHILD_OF");
              return {
                traceId: s.traceID,
                spanId: s.spanID,
                ...(parent ? { parentSpanId: parent.spanID } : {}),
                name: s.operationName,
                startTimeUnixNano: nanos(s.startTime),
                endTimeUnixNano: nanos(s.startTime + s.duration),
                attributes: attributes(s.tags),
                events: (s.logs ?? []).map((log) => ({
                  timeUnixNano: nanos(log.timestamp),
                  name: String((log.fields ?? []).find((f) => f.key === "event")?.value ?? ""),
                  attributes: attributes((log.fields ?? []).filter((f) => f.key !== "event")),
                })),
                links: (s.references ?? [])
                  .filter((r) => r.refType === "FOLLOWS_FROM")
                  .map((r) => ({ traceId: r.traceID, spanId: r.spanID })),
              };
            }),
          },
        ],
      })),
    },
  };
}

/** A `fetch` Response carrying `body` as JSON. */
export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
