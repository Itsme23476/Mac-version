import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
// Optional: until this secret is set, Turnstile checks are skipped so the form
// keeps working. Set it (see TURNSTILE_SECRET_KEY) to turn the captcha on.
const TURNSTILE_SECRET = Deno.env.get("TURNSTILE_SECRET_KEY");
const TO = "softwaregentofficial@gmail.com";
const FROM = "Filect Support <support@filect.io>";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Content-Type": "application/json",
};

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// --- Per-IP rate limit (backstop). In-memory, so it resets on cold start, but
// it's enough to stop one IP from hammering the form in a burst and torching
// the email quota. Honeypot + Turnstile do the heavy lifting. ---
const RATE_MAX = 3; // max submissions...
const RATE_WINDOW_MS = 60_000; // ...per IP per minute
const hits = new Map<string, number[]>();
function rateLimited(ip: string): boolean {
  const now = Date.now();
  const recent = (hits.get(ip) ?? []).filter((t) => now - t < RATE_WINDOW_MS);
  recent.push(now);
  hits.set(ip, recent);
  return recent.length > RATE_MAX;
}

// Verify the Cloudflare Turnstile token. Returns true (skips) until the secret
// is configured, so adding the captcha is a zero-downtime flip of one secret.
async function turnstileOk(token: string | undefined, ip: string): Promise<boolean> {
  if (!TURNSTILE_SECRET) return true;
  if (!token) return false;
  const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ secret: TURNSTILE_SECRET, response: token, remoteip: ip }),
  });
  const out = await r.json().catch(() => ({ success: false }));
  return out.success === true;
}

// Sends a contact-form submission to support@filect.io, with reply-to set to the
// sender so support can just hit reply.
serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim() || "unknown";
    const { name, email, message, company, turnstileToken } = await req.json();

    // 1) Honeypot: `company` is hidden from real users; bots fill every field.
    //    Pretend success so the bot moves on, but send/store nothing.
    if (company) {
      console.log("honeypot triggered, dropping submission from", ip);
      return new Response(JSON.stringify({ ok: true }), { headers: cors });
    }

    // 2) Basic validation
    if (!email || !message || message.trim().length < 10) {
      return new Response(JSON.stringify({ ok: false, error: "invalid" }), { status: 400, headers: cors });
    }

    // 3) Per-IP rate limit
    if (rateLimited(ip)) {
      return new Response(JSON.stringify({ ok: false, error: "rate_limited" }), { status: 429, headers: cors });
    }

    // 4) Turnstile (skipped until TURNSTILE_SECRET_KEY is set on this function)
    if (!(await turnstileOk(turnstileToken, ip))) {
      return new Response(JSON.stringify({ ok: false, error: "verification_failed" }), { status: 403, headers: cors });
    }

    // Store a copy so submissions are never lost even if the email fails.
    await supabase.from("contact_messages").insert({
      name: name || null, email, message: message.trim(),
    });

    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: FROM,
        to: TO,
        reply_to: email,
        subject: `New contact form message from ${name || email}`,
        html: `<p><strong>From:</strong> ${esc(name || "(no name)")} &lt;${esc(email)}&gt;</p>
               <p><strong>Message:</strong></p>
               <p>${esc(message).replace(/\n/g, "<br>")}</p>`,
      }),
    });

    if (!res.ok) {
      console.error("resend error:", await res.text());
      return new Response(JSON.stringify({ ok: false, error: "send_failed" }), { status: 502, headers: cors });
    }
    return new Response(JSON.stringify({ ok: true }), { headers: cors });
  } catch (e) {
    console.error("send-contact error:", e);
    return new Response(JSON.stringify({ ok: false, error: "server" }), { status: 500, headers: cors });
  }
});
