/**
 * Dashboard live streams through connect-proxy (#1542): the event feed
 * (StreamGameEvents) must use the Connect streaming wire format, or
 * connect-proxy answers 415 and the dashboard gets no live data (UC-035 main
 * scenario; UC-070 BR-005 follows a shadow game's stream the same way).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { gameClient } from "../client.js";
import {
  CONNECT_STREAM_CONTENT_TYPE,
  ConnectStreamError,
  END_STREAM_FLAG,
  decodeStream,
  encodeEnvelope,
  serverStream,
} from "../connect.js";

function frame(message: unknown, flags = 0): Uint8Array {
  return encodeEnvelope(message, flags);
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function streamResponse(chunks: Uint8Array[], init: ResponseInit = { status: 200 }): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
  return new Response(body, init);
}

async function* asChunks(chunks: Uint8Array[]): AsyncGenerator<Uint8Array> {
  yield* chunks;
}

async function collect<T>(gen: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

function decodeRequestEnvelope(body: Uint8Array): { flags: number; json: unknown } {
  const length = new DataView(body.buffer, body.byteOffset).getUint32(1, false);
  expect(body.length).toBe(5 + length);
  return { flags: body[0], json: JSON.parse(new TextDecoder().decode(body.slice(5))) };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UC-035: dashboard event stream uses the Connect streaming wire format (#1542)", () => {
  it("UC-035 main sends StreamGameEvents as application/connect+json with an enveloped request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamResponse([frame({ eventType: "game_start", gameId: "game_live" }), frame({}, END_STREAM_FLAG)])
    );
    vi.stubGlobal("fetch", fetchMock);

    const events = await collect(gameClient.streamGameEvents({ gameId: "game_live" }));

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/joustmania.game_coordinator.GameCoordinatorService/StreamGameEvents");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe(CONNECT_STREAM_CONTENT_TYPE);
    expect(decodeRequestEnvelope(init.body)).toEqual({ flags: 0, json: { gameId: "game_live" } });
    expect(events).toEqual([{ eventType: "game_start", gameId: "game_live" }]);
  });

  it("UC-035 main yields every message frame in order and ends cleanly on the end-stream frame", async () => {
    const events = await collect(
      decodeStream(asChunks([concat(frame({ timestamp: 1 }), frame({ timestamp: 2 }), frame({}, END_STREAM_FLAG))]))
    );
    expect(events).toEqual([{ timestamp: 1 }, { timestamp: 2 }]);
  });

  it("UC-035 main reassembles frames split across body chunks", async () => {
    const whole = concat(frame({ eventType: "countdown_start" }), frame({ eventType: "game_started" }), frame({}, END_STREAM_FLAG));
    // One byte per chunk: headers and payloads both straddle chunk boundaries.
    const chunks = Array.from(whole, (b) => Uint8Array.of(b));
    expect(await collect(decodeStream(asChunks(chunks)))).toEqual([
      { eventType: "countdown_start" },
      { eventType: "game_started" },
    ]);
  });

  it("UC-035 main surfaces the end-stream error after the messages before it", async () => {
    const seen: unknown[] = [];
    const run = async () => {
      for await (const msg of decodeStream(
        asChunks([
          frame({ timestamp: 1 }),
          frame({ error: { code: "internal", message: "coordinator gone" } }, END_STREAM_FLAG),
        ])
      )) {
        seen.push(msg);
      }
    };
    await expect(run()).rejects.toThrow(ConnectStreamError);
    await expect(
      collect(decodeStream(asChunks([frame({ error: { code: "internal", message: "coordinator gone" } }, END_STREAM_FLAG)])))
    ).rejects.toThrow(/internal: coordinator gone/);
    expect(seen).toEqual([{ timestamp: 1 }]);
  });

  it("UC-035 main treats a body that ends without an end-stream frame as a lost connection", async () => {
    await expect(collect(decodeStream(asChunks([frame({ timestamp: 1 })])))).rejects.toThrow(/end-stream/);
    const truncated = frame({ timestamp: 2 }).slice(0, 7);
    await expect(collect(decodeStream(asChunks([truncated])))).rejects.toThrow(/inside a frame/);
  });

  it("UC-035 main rejects a non-200 answer (the old 415) so the dashboard retries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("", { status: 415, statusText: "Unsupported Media Type" }));
    await expect(collect(serverStream("/x/Y", {}, undefined, fetchMock))).rejects.toThrow(/415/);
  });

  it("UC-070 BR-005 passes the abort signal through so following another game ends the stream", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([frame({}, END_STREAM_FLAG)]));
    vi.stubGlobal("fetch", fetchMock);
    const abort = new AbortController();

    await collect(gameClient.streamGameEvents({ gameId: "game_shadow" }, abort.signal));

    expect(fetchMock.mock.calls[0][1].signal).toBe(abort.signal);
  });
});
