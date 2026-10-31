/**
 * Connect streaming wire format for the dashboard's server-streaming calls
 * (#1542). connect-proxy rejects a plain `application/json` POST to a
 * streaming procedure with 415, so a stream request is sent as
 * `application/connect+json` with every message in a 5-byte envelope:
 *
 *   byte 0     flags (0x02 = end-stream frame)
 *   bytes 1-4  big-endian payload length
 *   bytes 5-   JSON payload
 *
 * The response is a sequence of such frames; the last one is the end-stream
 * frame, whose JSON may carry `{"error": {"code", "message"}}`.
 */

export const CONNECT_STREAM_CONTENT_TYPE = "application/connect+json";
export const END_STREAM_FLAG = 0b00000010;
const COMPRESSED_FLAG = 0b00000001;
const HEADER_BYTES = 5;

export class ConnectStreamError extends Error {
  constructor(
    readonly code: string,
    message: string
  ) {
    super(`RPC failed: ${code}${message ? `: ${message}` : ""}`);
    this.name = "ConnectStreamError";
  }
}

/**
 * The error of a failed unary call (#2387): a Connect error body is
 * `{"code": "unavailable", "message": "..."}`; keep both instead of the bare HTTP
 * status. A body that is not Connect JSON (a proxy's HTML page) gives
 * `http_<status>` and the status text.
 */
export async function unaryRpcError(response: Pick<Response, "status" | "statusText" | "text">): Promise<ConnectStreamError> {
  let code = `http_${response.status}`;
  let message = response.statusText;
  try {
    const body = JSON.parse(await response.text());
    if (body && typeof body.code === "string" && body.code) {
      code = body.code;
      message = typeof body.message === "string" ? body.message : "";
    }
  } catch {
    // not JSON: keep the HTTP status
  }
  return new ConnectStreamError(code, message);
}

export interface Frame {
  flags: number;
  payload: Uint8Array;
}

/** Wrap one JSON message in a Connect envelope. */
export function encodeEnvelope(message: unknown, flags = 0): Uint8Array<ArrayBuffer> {
  const payload = new TextEncoder().encode(JSON.stringify(message));
  const frame = new Uint8Array(HEADER_BYTES + payload.length);
  frame[0] = flags;
  new DataView(frame.buffer).setUint32(1, payload.length, false);
  frame.set(payload, HEADER_BYTES);
  return frame;
}

/**
 * Split byte chunks into envelopes. A frame may span several chunks and one
 * chunk may hold several frames. Throws when the body ends inside a frame.
 */
export async function* readFrames(chunks: AsyncIterable<Uint8Array>): AsyncGenerator<Frame> {
  let buffer = new Uint8Array(0);
  let offset = 0;
  for await (const chunk of chunks) {
    // Keep only the unread tail, then append: each byte is copied once per
    // chunk it waits through, not once per frame.
    const unread = buffer.length - offset;
    const joined = new Uint8Array(unread + chunk.length);
    joined.set(buffer.subarray(offset));
    joined.set(chunk, unread);
    buffer = joined;
    offset = 0;

    while (buffer.length - offset >= HEADER_BYTES) {
      const length = new DataView(buffer.buffer, buffer.byteOffset + offset).getUint32(1, false);
      const end = offset + HEADER_BYTES + length;
      if (buffer.length < end) break;
      yield { flags: buffer[offset], payload: buffer.subarray(offset + HEADER_BYTES, end) };
      offset = end;
    }
  }
  if (buffer.length - offset > 0) {
    throw new ConnectStreamError("internal", "stream ended inside a frame");
  }
}

async function* bodyChunks(body: ReadableStream<Uint8Array>): AsyncGenerator<Uint8Array> {
  const reader = body.getReader();
  let finished = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        finished = true;
        return;
      }
      if (value) yield value;
    }
  } finally {
    // Early exit (parse error, consumer break): close the HTTP connection.
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

/**
 * Decode response frames into messages. Ends cleanly on an end-stream frame
 * without an error, throws ConnectStreamError on one with an error, and also
 * throws when the body ends without an end-stream frame (connection cut).
 */
export async function* decodeStream<Res>(chunks: AsyncIterable<Uint8Array>): AsyncGenerator<Res> {
  const decoder = new TextDecoder();
  for await (const frame of readFrames(chunks)) {
    if (frame.flags & COMPRESSED_FLAG) {
      throw new ConnectStreamError("internal", "compressed frame not supported");
    }
    const text = decoder.decode(frame.payload);
    if (frame.flags & END_STREAM_FLAG) {
      const end = text ? JSON.parse(text) : {};
      if (end.error) {
        throw new ConnectStreamError(end.error.code ?? "unknown", end.error.message ?? "");
      }
      return;
    }
    yield JSON.parse(text) as Res;
  }
  throw new ConnectStreamError("unavailable", "stream ended without an end-stream frame");
}

/** POST one request to a Connect server-streaming procedure and yield its messages. */
export async function* serverStream<Req, Res>(
  url: string,
  request: Req,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch
): AsyncGenerator<Res> {
  const response = await fetchImpl(url, {
    method: "POST",
    headers: {
      "Content-Type": CONNECT_STREAM_CONTENT_TYPE,
      "Connect-Protocol-Version": "1",
    },
    body: encodeEnvelope(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`RPC failed: ${response.status} ${response.statusText}`);
  }
  if (!response.body) {
    throw new Error("No response body");
  }

  yield* decodeStream<Res>(bodyChunks(response.body));
}
