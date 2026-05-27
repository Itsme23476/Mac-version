import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const TO = "softwaregentofficial@gmail.com";
const FROM = "Filect Contact <team@filect.io>";

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

// Sends a contact-form submission to support@filect.io, with reply-to set to the
// sender so support can just hit reply.
serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const { name, email, message } = await req.json();
    if (!email || !message || message.trim().length < 10) {
      return new Response(JSON.stringify({ ok: false, error: "invalid" }), { status: 400, headers: cors });
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
