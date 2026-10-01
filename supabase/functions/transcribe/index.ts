import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// ---------------------------------------------------------------------------
// Filect Voice — server-side transcription proxy (Grok Voice Transcribe 2.0).
//
// The client records audio and POSTs it here as base64 together with the user's
// Supabase JWT. We:
//   1) verify the user,
//   2) gate on the SAME get_entitlement RPC the app + openai-proxy use (fail closed),
//   3) enforce a per-user daily fair-use ceiling (abuse guard),
//   4) forward the audio to xAI's /v1/stt with key-term biasing ("Filect"),
//   5) log the real audio-seconds for accurate cost tracking + the ceiling.
//
// The XAI_API_KEY never leaves the server. Deploy with --no-verify-jwt: we do
// auth ourselves (same as openai-proxy) via supabase.auth.getUser(token).
// ---------------------------------------------------------------------------

const XAI_API_KEY = Deno.env.get("XAI_API_KEY");
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const XAI_STT_URL = "https://api.x.ai/v1/stt";
const XAI_MODEL = "grok-voice-transcribe-2.0";
// xAI STT key-term biasing: repeat the `keyterm` form field once per term.
// Confirmed against docs.x.ai speech-to-text — field name "keyterm", up to 100
// terms, each <=50 chars. "Filect" is always biased so it stops becoming "file liked".
const KEYTERM_FIELD = "keyterm";
const BASE_KEYTERMS = ["Filect"];
const MAX_KEYTERMS = 100; // xAI per-request ceiling
const MAX_KEYTERM_LEN = 50; // xAI per-term char ceiling

// Normalize a raw term source (array or comma-separated string): trim, drop empties
// and over-long entries, de-dupe case-insensitively.
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
// Fair-use ceiling — generous enough that only abuse hits it. Tune here.
const DAILY_LIMIT_SECONDS = 3 * 60 * 60; // 3 hours of audio per user per UTC day

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, content-type, x-client-info, apikey, x-audio-filename, x-audio-language, x-audio-terms",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...cors },
  });
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: cors });

  try {
    const authHeader = req.headers.get("Authorization");
    if (!authHeader) return json({ error: "No authorization header" }, 401);
    const token = authHeader.replace("Bearer ", "");

    // 1) Who is this?
    const { data: { user }, error: authError } = await supabase.auth.getUser(token);
    if (authError || !user) return json({ error: "Invalid token" }, 401);

    // 2+3) Entitlement AND today's usage in ONE parallel round-trip (both only need
    // user.id) — saves a serial hop. Same entitlement source of truth as openai-proxy;
    // fail closed on error.
    const today = new Date().toISOString().slice(0, 10); // UTC yyyy-mm-dd
    const [entRes, usageRes] = await Promise.all([
      supabase.rpc("get_entitlement", { p_user_id: user.id }),
      supabase.from("voice_usage").select("seconds").eq("user_id", user.id).eq("day", today),
    ]);
    if (entRes.error) {
      console.error("Entitlement check error:", entRes.error);
      return json({ error: "Entitlement check failed" }, 403);
    }
    const ent = Array.isArray(entRes.data) ? entRes.data[0] : entRes.data;
    if (!ent?.entitled) return json({ error: "No active subscription" }, 403);

    // ponytail: sums the day's rows in JS; swap for an aggregate RPC only if a heavy
    // user's daily row count ever gets large enough to matter.
    const usedSeconds = (usageRes.data ?? []).reduce(
      (s, r) => s + Number(r.seconds || 0),
      0,
    );
    if (usedSeconds >= DAILY_LIMIT_SECONDS) {
      return json({ error: "Daily voice limit reached", reason: "rate_limited" }, 429);
    }

    // 4) Read the audio. New clients send a RAW binary body (application/octet-stream,
    // metadata in headers) — no base64, ~33% smaller upload. Older release clients send
    // base64 JSON. Support BOTH so deploying this doesn't break users who haven't
    // updated their app yet.
    let bytes: Uint8Array;
    let filename: string;
    let language: string | undefined;
    let terms: string[] = []; // user's custom words (key-term biasing)
    if ((req.headers.get("content-type") || "").includes("application/json")) {
      const body = await req.json();
      const b64 = body.audio_base64 as string | undefined;
      if (!b64) return json({ error: "Missing audio_base64" }, 400);
      bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      filename = (body.audio_filename as string) || "audio.wav";
      language = body.language as string | undefined;
      terms = cleanTerms(body.terms); // JSON body: `terms` array
    } else {
      bytes = new Uint8Array(await req.arrayBuffer());
      if (bytes.length === 0) return json({ error: "Missing audio body" }, 400);
      filename = req.headers.get("x-audio-filename") || "audio.flac";
      language = req.headers.get("x-audio-language") || undefined; // xAI auto-detects
      terms = cleanTerms(req.headers.get("x-audio-terms")); // raw path: comma-separated header
    }

    // 5) Forward to xAI Grok Voice Transcribe 2.0 (multipart/form-data).
    const form = new FormData();
    form.append("file", new Blob([bytes]), filename);
    form.append("model", XAI_MODEL);
    // Key-term biasing: base brand term(s) + the user's custom words, repeated field,
    // de-duped across both and capped at xAI's ceiling. Empty terms → base only
    // (identical to previous behavior).
    const seen = new Set<string>();
    for (const kt of [...BASE_KEYTERMS, ...terms]) {
      const k = kt.toLowerCase();
      if (seen.has(k)) continue;
      seen.add(k);
      form.append(KEYTERM_FIELD, kt);
      if (seen.size >= MAX_KEYTERMS) break;
    }
    if (language) form.append("language", language);

    const xaiRes = await fetch(XAI_STT_URL, {
      method: "POST",
      headers: { "Authorization": `Bearer ${XAI_API_KEY}` },
      body: form,
    });
    if (!xaiRes.ok) {
      const detail = await xaiRes.text();
      console.error("xAI STT error:", xaiRes.status, detail);
      return json({ error: "Transcription provider error", details: detail }, xaiRes.status);
    }
    const data = await xaiRes.json();
    const text = (data.text || "").trim();
    const duration = Number(data.duration || 0); // xAI returns audio length in seconds

    // 6) Log real audio-seconds AFTER replying — keep the DB write OFF the response
    // critical path (fire-and-forget via EdgeRuntime.waitUntil so it adds no latency).
    if (duration > 0) {
      const logPromise = supabase
        .from("voice_usage")
        .insert({ user_id: user.id, day: today, seconds: duration })
        .then(({ error }) => {
          if (error) console.error("voice_usage log failed:", error);
        });
      // EdgeRuntime is provided by the Supabase Edge runtime; the promise still runs
      // in the background if it's somehow unavailable.
      (globalThis as any).EdgeRuntime?.waitUntil?.(logPromise);
    }

    return json({ text, duration });
  } catch (e) {
    console.error("transcribe error:", e);
    return json({ error: "Internal server error", details: String(e) }, 500);
  }
});
