// Deploy: supabase functions deploy clean-transcript --no-verify-jwt --project-ref gsvccxhdgcshiwgjvgfi
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// ---------------------------------------------------------------------------
// Filect Voice — dictation transcript cleanup.
//
// Optional "AI Cleanup" toggle: lightly polishes a RAW dictation transcript
// (drop filler, fix stumbles/punctuation) WITHOUT changing meaning. In the app,
// Off = raw verbatim; this is only called when the user opts in.
//
// Entitlement-gated (active subscription). No monthly cap — cleanup is pennies.
// Robust by design: on ANY failure we return the ORIGINAL text so dictation
// never breaks because cleanup failed. The OpenAI key never leaves the server.
// Deploy --no-verify-jwt (the function validates the bearer token itself).
// ---------------------------------------------------------------------------

const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY");
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const OPENAI_URL = "https://api.openai.com/v1/chat/completions";
const MODEL = "gpt-4o-mini";

// Two polish levels (the "none" level never reaches the server — the client skips the
// call). LIGHT is conservative; POLISHED additionally smooths phrasing.
const LIGHT_PROMPT =
  "You clean up a raw voice-dictation transcript. Remove filler words (um, uh, " +
  "like, you know). Fix false starts, self-corrections and obvious stumbles. Fix " +
  "capitalization, punctuation and clear grammar errors. Do NOT change the meaning. " +
  "Do NOT add or remove information. Do NOT rewrite the style or make it more " +
  "formal. Return ONLY the cleaned text — no preamble, quotes or explanation.";
const POLISHED_PROMPT =
  "You polish a raw voice-dictation transcript into clean written text. Remove filler " +
  "words, false starts, self-corrections and stumbles; fix punctuation, capitalization " +
  "and grammar; and smooth awkward or run-on phrasing so it reads clearly, lightly " +
  "rephrasing where needed. Keep the original meaning and ALL the information — do NOT " +
  "add new ideas, do NOT drop any point, and do NOT make it more formal than the " +
  "speaker. Return ONLY the polished text — no preamble, quotes or explanation.";
const PROMPTS: Record<string, string> = { light: LIGHT_PROMPT, polished: POLISHED_PROMPT };

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

  let rawText = "";
  try {
    const authHeader = req.headers.get("Authorization");
    if (!authHeader) return json({ error: "No authorization header" }, 401);
    const token = authHeader.replace("Bearer ", "");

    const body = await req.json().catch(() => ({}));
    rawText = ((body.text as string) || "").trim();
    if (!rawText) return json({ text: "" }); // nothing to clean
    const level = ((body.level as string) || "light").toLowerCase();
    let systemPrompt = PROMPTS[level] || LIGHT_PROMPT; // unknown level -> conservative
    // The user's Custom Words — the cleanup must NEVER "correct" these proper nouns
    // (e.g. 'Filect' -> 'Firefox'). Pin them in the prompt.
    const terms = Array.isArray(body.terms)
      ? (body.terms as unknown[]).map((t) => String(t).trim()).filter(Boolean).slice(0, 200)
      : [];
    if (terms.length) {
      systemPrompt += " IMPORTANT — the speaker frequently uses these exact terms: " +
        terms.join(", ") + ". Keep them EXACTLY as written when present. AND if the " +
        "transcript contains a clear mis-transcription of one of them — a similar-sounding " +
        "word or short phrase (for example 'file liked' or 'Firefox' when the term is " +
        "'Filect') — replace it with the correct term. Only do this when it is clearly " +
        "meant to be one of these terms; never force an unrelated word into one.";
    }

    // Who is this?
    const { data: { user }, error: authError } = await supabase.auth.getUser(token);
    if (authError || !user) return json({ error: "Invalid token" }, 401);

    // Entitlement gate. Degrade to raw text rather than breaking dictation.
    const entRes = await supabase.rpc("get_entitlement", { p_user_id: user.id });
    if (entRes.error) return json({ text: rawText });
    const ent = Array.isArray(entRes.data) ? entRes.data[0] : entRes.data;
    if (!ent?.entitled) return json({ text: rawText }); // not entitled → verbatim

    if (!OPENAI_API_KEY) {
      console.error("OPENAI_API_KEY not set");
      return json({ text: rawText }); // graceful fallback
    }

    const aiRes = await fetch(OPENAI_URL, {
      method: "POST",
      headers: { "Authorization": `Bearer ${OPENAI_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: MODEL,
        temperature: 0.2,
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: rawText },
        ],
      }),
    });
    if (!aiRes.ok) {
      console.error("OpenAI error:", aiRes.status, await aiRes.text());
      return json({ text: rawText }); // graceful fallback
    }
    const data = await aiRes.json();
    const cleaned = (data.choices?.[0]?.message?.content || "").trim();
    return json({ text: cleaned || rawText });
  } catch (e) {
    console.error("clean-transcript error:", e);
    // Never fail dictation over cleanup — return the raw text.
    return json({ text: rawText });
  }
});
