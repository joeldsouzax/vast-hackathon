// Gemini SSE relay. Keys stay in Supabase secrets; callers cannot choose a host.
const ORIGIN = "https://generativelanguage.googleapis.com/v1beta";
const MAX_REQUEST = 20 * 1024 * 1024;
const MAX_RESPONSE = 8 * 1024 * 1024;
declare const EdgeRuntime: { waitUntil(promise: Promise<unknown>): void };

function failure(status: number, code: string): Response {
  return Response.json({ error: { code } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

async function authorized(received: string, expected: string): Promise<boolean> {
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(expected) || received.length > 128) return false;
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(received)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const aa = new Uint8Array(a), bb = new Uint8Array(b);
  let difference = 0;
  for (let i = 0; i < aa.length; i++) difference |= aa[i] ^ bb[i];
  return difference === 0;
}

async function boundedJSON(request: Request): Promise<Record<string, unknown>> {
  if (!request.body) throw new Error("invalid_request");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > MAX_REQUEST) throw new Error("request_too_large");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel();
    throw error;
  } finally {
    reader.releaseLock();
  }
  const combined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { combined.set(chunk, offset); offset += chunk.length; }
  const value = JSON.parse(new TextDecoder().decode(combined));
  if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("invalid_request");
  return value;
}

export async function handler(request: Request): Promise<Response> {
  if (request.method !== "POST") return failure(405, "method_not_allowed");
  const runtimeKey = Deno.env.get("BREADCAST_RUNTIME_KEY") || "";
  const geminiKey = Deno.env.get("GEMINI_API_KEY") || "";
  if (!runtimeKey || !geminiKey) return failure(503, "configuration_missing");
  if (!await authorized(request.headers.get("x-breadcast-runtime-key") || "", runtimeKey)) {
    return failure(401, "auth_failed");
  }
  let input: Record<string, unknown>;
  try { input = await boundedJSON(request); }
  catch (error) {
    return failure(error instanceof Error && error.message === "request_too_large" ? 413 : 400, "invalid_request");
  }
  const operation = input.operation;
  if (!["models", "streamGenerateContent", "embedContent"].includes(String(operation))) {
    return failure(400, "invalid_operation");
  }
  const url = new URL(operation === "models" ? ORIGIN + "/models" :
    ORIGIN + "/models/" + input.model + ":" + operation);
  if (operation !== "models" && (typeof input.model !== "string" ||
    !/^gemini-[A-Za-z0-9._-]{1,184}$/.test(input.model))) return failure(400, "invalid_model");
  if (operation === "streamGenerateContent") url.searchParams.set("alt", "sse");
  if (operation === "models") {
    url.searchParams.set("pageSize", "1000");
    const params = input.params as Record<string, unknown> | undefined;
    if (typeof params?.pageToken === "string" && params.pageToken.length <= 4096) {
      url.searchParams.set("pageToken", params.pageToken);
    }
  } else if (!input.payload || typeof input.payload !== "object" || Array.isArray(input.payload)) {
    return failure(400, "invalid_payload");
  }
  const abort = new AbortController();
  const signal = AbortSignal.any([abort.signal, request.signal, AbortSignal.timeout(120_000)]);
  try {
    const upstream = await fetch(url, {
      method: operation === "models" ? "GET" : "POST",
      headers: { "x-goog-api-key": geminiKey, "Content-Type": "application/json" },
      body: operation === "models" ? undefined : JSON.stringify(input.payload),
      redirect: "error", signal,
    });
    if (!upstream.ok) {
      await upstream.body?.cancel();
      return failure(upstream.status, "gemini_request_failed");
    }
    if (!upstream.body) return failure(502, "invalid_response");
    const headers = new Headers({
      "Content-Type": operation === "streamGenerateContent" ? "text/event-stream" : "application/json",
      "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
    });
    if (operation === "streamGenerateContent" &&
      !upstream.headers.get("content-type")?.includes("text/event-stream")) {
      await upstream.body.cancel();
      return failure(502, "invalid_response");
    }
    const requestID = upstream.headers.get("x-request-id");
    if (requestID && /^[A-Za-z0-9_.:\-]{1,128}$/.test(requestID)) headers.set("x-request-id", requestID);
    // Read only when the caller requests another chunk. No full-response buffer.
    const reader = upstream.body.getReader();
    let bytes = 0;
    let complete!: () => void;
    const lifetime = new Promise<void>((resolve) => { complete = resolve; });
    if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(lifetime);
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { value, done } = await reader.read();
          if (done) { controller.close(); reader.releaseLock(); complete(); return; }
          bytes += value.length;
          if (bytes > MAX_RESPONSE) {
            abort.abort(); await reader.cancel(); complete(); controller.error(new Error("response_too_large")); return;
          }
          controller.enqueue(value);
        } catch {
          abort.abort(); complete(); controller.error(new Error("gemini_stream_failed"));
        }
      },
      async cancel() { abort.abort(); complete(); await reader.cancel(); },
    });
    return new Response(stream, { status: 200, headers });
  } catch {
    abort.abort();
    return failure(signal.aborted ? 504 : 502, "gemini_unavailable");
  }
}

Deno.serve(handler);
