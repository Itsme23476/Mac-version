// deno-lint-ignore-file no-explicit-any
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import WS from "npm:ws@8.18.0";

// ---------------------------------------------------------------------------
// Filect Voice — STREAMING transcription proxy (Grok Voice Transcribe 2.0).
//
// Same security model as `transcribe` (batch), just over a WebSocket so the
// transcript is ready the instant the user releases the key:
//   1) verify the user (JWT in the Authorization header or ?token=),
//   2) gate on get_entitlement (fail closed) + the per-user daily seconds ceiling,
//   3) upgrade the CLIENT websocket and open an UPSTREAM websocket to xAI's
//      wss://api.x.ai/v1/stt WITH the Authorization header (via npm:ws, because
//      Supabase's Deno runtime has no WebSocketStream and native WebSocket can't
//      send headers),
//   4) pipe client PCM chunks up and relay xAI's transcript events down verbatim,
//   5) log the real audio-seconds on close (same voice_usage table/ceiling).
//
// The XAI_API_KEY never leaves the server. The batch `transcribe` function is
// untouched and remains the client's automatic fallback.
// Deploy with --no-verify-jwt (we auth ourselves, same as transcribe/openai-proxy).
// ---------------------------------------------------------------------------

const XAI_API_KEY = Deno.env.get("XAI_API_KEY") || "";
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const XAI_WS_BASE = "wss://api.x.ai/v1/stt";
const XAI_MODEL = "grok-voice-transcribe-2.0";
const KEYTERM_FIELD = "keyterm";
const BASE_KEYTERMS = ["Filect"];
const MAX_KEYTERMS = 100;
const MAX_KEYTERM_LEN = 50;
const DAILY_LIMIT_SECONDS = 3 * 60 * 60; // 3h/user/day — mirrors `transcribe`
const SAMPLE_RATE = 16000; // for audio-seconds accounting (int16 mono)

function cleanTerms(raw: unknown): string[] {
  const arr = Array.isArray(raw)
    ? raw
    : typeof raw === "string"
    ? raw.split(",")
    : [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of arr) {
    const t = String(item ?? "").trim();
    if (!t || t.length > MAX_KEYTERM_LEN) continue;
    const k = t.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(t);
  }
  return out;
}

Deno.serve(async (req) => {
  const url = new URL(req.url);

  // --- 1) auth (header or ?token=) -----------------------------------------
  const authHeader = req.headers.get("Authorization");
  const token = (authHeader ? authHeader.replace("Bearer ", "") : "") ||
    url.searchParams.get("token") || "";
  if (!token) return new Response("No authorization", { status: 401 });

  const { data: { user }, error: authError } = await supabase.auth.getUser(token);
  if (authError || !user) return new Response("Invalid token", { status: 401 });

  // --- 2) entitlement + daily ceiling (fail closed) ------------------------
  const today = new Date().toISOString().slice(0, 10);
  const [entRes, usageRes] = await Promise.all([
    supabase.rpc("get_entitlement", { p_user_id: user.id }),
    supabase.from("voice_usage").select("seconds").eq("user_id", user.id).eq("day", today),
  ]);
  if (entRes.error) return new Response("Entitlement check failed", { status: 403 });
  const ent = Array.isArray(entRes.data) ? entRes.data[0] : entRes.data;
  if (!ent?.entitled) return new Response("No active subscription", { status: 403 });
  const usedSeconds = (usageRes.data ?? []).reduce((s, r) => s + Number(r.seconds || 0), 0);
  if (usedSeconds >= DAILY_LIMIT_SECONDS) {
    return new Response("Daily voice limit reached", { status: 429 });
  }

  // --- 3) build the xAI stream URL (model + language + key-term biasing) ----
  const language = url.searchParams.get("language") || "";
  const terms = cleanTerms(url.searchParams.get("terms"));
  const params = [
    `sample_rate=${SAMPLE_RATE}`,
    "encoding=pcm",
    "interim_results=true",
    `model=${XAI_MODEL}`,
  ];
  if (language) params.push(`language=${encodeURIComponent(language)}`);
  const seen = new Set<string>();
  for (const kt of [...BASE_KEYTERMS, ...terms]) {
    const k = kt.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    params.push(`${KEYTERM_FIELD}=${encodeURIComponent(kt)}`);
    if (seen.size >= MAX_KEYTERMS) break;
  }
  const xaiUrl = `${XAI_WS_BASE}?${params.join("&")}`;

  // --- 4) upgrade the client WS; open the upstream xAI WS -------------------
  let client: WebSocket;
  let response: Response;
  try {
    ({ socket: client, response } = Deno.upgradeWebSocket(req));
  } catch (_e) {
    return new Response("Expected a WebSocket upgrade", { status: 426 });
  }
  client.binaryType = "arraybuffer";

  const upstream: any = new WS(xaiUrl, { headers: { Authorization: `Bearer ${XAI_API_KEY}` } });
  let upstreamOpen = false;
  let totalBytes = 0;
  const pending: any[] = [];   // binary audio chunks AND text control frames, in order
  let logged = false;

  const logUsage = () => {
    if (logged) return;
    logged = true;
    const seconds = totalBytes / (2 * SAMPLE_RATE); // int16 mono → bytes/2 = samples
    if (seconds > 0.1) {
      const p = supabase.from("voice_usage")
        .insert({ user_id: user.id, day: today, seconds })
        .then(({ error }: any) => { if (error) console.error("voice_usage log failed:", error); });
      (globalThis as any).EdgeRuntime?.waitUntil?.(p);
    }
  };

  const closeBoth = (code = 1000, reason = "") => {
    try { if (client.readyState === WebSocket.OPEN) client.close(code, reason.slice(0, 120)); } catch (_e) { /* noop */ }
    try { upstream.close(); } catch (_e) { /* noop */ }
    logUsage();
  };

  upstream.on("open", () => {
    upstreamOpen = true;
    for (const p of pending) { try { upstream.send(p); } catch (_e) { /* noop */ } }
    pending.length = 0;
    try { client.send(JSON.stringify({ type: "ready" })); } catch (_e) { /* noop */ }
  });
  upstream.on("message", (data: any, isBinary: boolean) => {
    // xAI sends JSON text events; relay them verbatim to the client.
    try {
      if (client.readyState === WebSocket.OPEN) {
        client.send(isBinary ? data : data.toString());
      }
    } catch (_e) { /* noop */ }
  });
  upstream.on("error", (e: any) => {
    try { client.send(JSON.stringify({ type: "error", message: String(e?.message || e) })); } catch (_e) { /* noop */ }
    closeBoth(1011, "upstream error");
  });
  upstream.on("unexpected-response", (_req: any, resp: any) => {
    try { client.send(JSON.stringify({ type: "error", message: "xai_status_" + resp.statusCode })); } catch (_e) { /* noop */ }
    closeBoth(1011, "xai " + resp.statusCode);
  });
  upstream.on("close", () => { closeBoth(); });

  client.onmessage = (ev: MessageEvent) => {
    const d: any = ev.data;
    if (typeof d === "string") {
      // Control message from the client — e.g. {"type":"audio.done"} on key-release,
      // which tells xAI to flush its buffer and emit the final, fully-stitched
      // transcript.done. Forward it verbatim (xAI ignores unknown types).
      if (upstreamOpen) { try { upstream.send(d); } catch (_e) { /* noop */ } }
      else pending.push(d);
      return;
    }
    const u8 = new Uint8Array(d as ArrayBuffer);
    totalBytes += u8.byteLength;
    if (upstreamOpen) { try { upstream.send(u8); } catch (_e) { /* noop */ } }
    else pending.push(u8);
  };
  client.onclose = () => { try { upstream.close(); } catch (_e) { /* noop */ } logUsage(); };
  client.onerror = () => { try { upstream.close(); } catch (_e) { /* noop */ } logUsage(); };

  return response;
});
