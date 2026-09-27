import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// ---------------------------------------------------------------------------
// Filect Voice — search-query distiller.
//
// Voice search transcribes a spoken sentence ("can you dig up the invoice I got
// from the gas company last month"), which is a poor keyword query. This turns it
// into a tight search query ("gas invoice") with an LLM so the app's file search
// returns relevant hits regardless of phrasing. Deploy with --no-verify-jwt.
//
// Cost guard: per-user MONTHLY cap on distill calls (voice_search_usage). Over the
// cap we return the raw text so search still works (just without LLM cleaning).
// The OpenAI key never leaves the server.
// ---------------------------------------------------------------------------

const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY");
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const OPENAI_URL = "https://api.openai.com/v1/chat/completions";
const MODEL = "gpt-4o-mini";
const MONTHLY_LIMIT = 50_000;              // distill calls per user per month
const SYSTEM_PROMPT =
  "You convert a spoken request to find a file into a short keyword search query. " +
  "Reply with ONLY the search keywords — no punctuation, no quotes, no explanation. " +
  "Keep names, brands, topics, dates and file types; drop conversational filler like " +
  "'can you', 'find me', 'that I have', 'on my computer'. If it's already just keywords, " +
  "return them unchanged.";

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
    if (!rawText) return json({ error: "Missing text" }, 400);

    // Who is this? (also gate on entitlement + read this month's usage in parallel)
    const { data: { user }, error: authError } = await supabase.auth.getUser(token);
    if (authError || !user) return json({ error: "Invalid token" }, 401);

    const month = new Date().toISOString().slice(0, 7); // 'YYYY-MM' UTC
    const [entRes, usageRes] = await Promise.all([
      supabase.rpc("get_entitlement", { p_user_id: user.id }),
      supabase.from("voice_search_usage").select("count").eq("user_id", user.id).eq("month", month),
    ]);
    if (entRes.error) return json({ error: "Entitlement check failed" }, 403);
    const ent = Array.isArray(entRes.data) ? entRes.data[0] : entRes.data;
    if (!ent?.entitled) return json({ error: "No active subscription" }, 403);

    const used = (usageRes.data?.[0]?.count as number) ?? 0;
    if (used >= MONTHLY_LIMIT) {
      // Over the monthly cap — degrade gracefully: search on the raw transcript.
      return json({ query: rawText, distilled: false, capped: true });
    }

    if (!OPENAI_API_KEY) {
      console.error("OPENAI_API_KEY not set");
      return json({ query: rawText, distilled: false });   // graceful fallback
    }

    const aiRes = await fetch(OPENAI_URL, {
      method: "POST",
      headers: { "Authorization": `Bearer ${OPENAI_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: MODEL,
        temperature: 0,
        max_tokens: 32,
        messages: [
          { role: "system", content: SYSTEM_PROMPT },
          { role: "user", content: rawText },
        ],
      }),
    });
    if (!aiRes.ok) {
      console.error("OpenAI error:", aiRes.status, await aiRes.text());
      return json({ query: rawText, distilled: false });    // graceful fallback
    }
    const data = await aiRes.json();
    const distilled = (data.choices?.[0]?.message?.content || "").trim();
    const query = distilled || rawText;

    // Count the successful distill (fire-and-forget so it's off the response path).
    const inc = supabase.rpc("increment_voice_search", { p_user_id: user.id, p_month: month })
      .then(({ error }: { error: unknown }) => { if (error) console.error("usage inc failed:", error); });
    (globalThis as any).EdgeRuntime?.waitUntil?.(inc);

    return json({ query, distilled: true });
  } catch (e) {
    console.error("distill-query error:", e);
    // Never fail the search over distillation — return the raw text.
    return json({ query: rawText, distilled: false });
  }
});
