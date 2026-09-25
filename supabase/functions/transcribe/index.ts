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
// Bias the model toward our own vocabulary so "Filect" stops becoming "file liked".
const KEYTERMS = "Filect";
// Fair-use ceiling — generous enough that only abuse hits it. Tune here.
const DAILY_LIMIT_SECONDS = 3 * 60 * 60; // 3 hours of audio per user per UTC day

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, content-type, x-client-info, apikey",
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

    // 2) Entitled? Same source of truth as openai-proxy — fail closed on error.
    const { data: entRows, error: entError } = await supabase.rpc("get_entitlement", {
      p_user_id: user.id,
    });
    if (entError) {
      console.error("Entitlement check error:", entError);
      return json({ error: "Entitlement check failed" }, 403);
    }
    const ent = Array.isArray(entRows) ? entRows[0] : entRows;
    if (!ent?.entitled) return json({ error: "No active subscription" }, 403);

    // 3) Fair-use ceiling: sum today's audio-seconds for this user.
    // ponytail: sums the day's rows in JS; swap for an aggregate RPC only if a
    // heavy user's daily row count ever gets large enough to matter.
    const today = new Date().toISOString().slice(0, 10); // UTC yyyy-mm-dd
    const { data: usageRows } = await supabase
      .from("voice_usage")
      .select("seconds")
      .eq("user_id", user.id)
      .eq("day", today);
    const usedSeconds = (usageRows ?? []).reduce(
      (s, r) => s + Number(r.seconds || 0),
      0,
    );
    if (usedSeconds >= DAILY_LIMIT_SECONDS) {
      return json({ error: "Daily voice limit reached", reason: "rate_limited" }, 429);
    }

    // 4) Decode the audio the client sent.
    const body = await req.json();
    const b64 = body.audio_base64 as string | undefined;
    if (!b64) return json({ error: "Missing audio_base64" }, 400);
    const filename = (body.audio_filename as string) || "audio.wav";
    const language = body.language as string | undefined; // optional; xAI auto-detects
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

    // 5) Forward to xAI Grok Voice Transcribe 2.0 (multipart/form-data).
    const form = new FormData();
    form.append("file", new Blob([bytes]), filename);
    form.append("model", XAI_MODEL);
    form.append("keyterm", KEYTERMS);
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

    // 6) Log real audio-seconds (accurate cost + drives the fair-use ceiling).
    if (duration > 0) {
      const { error: logErr } = await supabase.from("voice_usage").insert({
        user_id: user.id,
        day: today,
        seconds: duration,
      });
      if (logErr) console.error("voice_usage log failed:", logErr);
    }

    return json({ text, duration });
  } catch (e) {
    console.error("transcribe error:", e);
    return json({ error: "Internal server error", details: String(e) }, 500);
  }
});
