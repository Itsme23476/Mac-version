import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Sends a single payment-failed recovery email with a one-click link that opens
// the user's Stripe Customer Portal (so they can update their card).
// Idempotent per user per kind — if we already sent this kind in the last 24h
// we skip, so retrying the webhook or backfill is safe.

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const FROM = "Filect Support <support@filect.io>";

const supabase = createClient(
  SUPABASE_URL,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, authorization",
  "Content-Type": "application/json",
};

function buildEmail(firstName: string | null, portalUrl: string, isReminder: boolean): { subject: string; html: string } {
  const greeting = firstName ? `Hi ${firstName.split(" ")[0]},` : "Hi there,";
  const subject = isReminder
    ? "Reminder: your Filect payment didn't go through"
    : "We couldn't process your Filect payment";
  const opener = isReminder
    ? "Just a quick reminder — we tried to charge your card a few days ago and it didn't go through. Your subscription is still paused until the payment is sorted."
    : "We tried to charge your card for your Filect subscription and it didn't go through. This usually means your card expired, was replaced, or hit a limit.";
  return {
    subject,
    html: `<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:40px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
        <tr><td style="background:#7C4DFF;padding:32px 40px;text-align:center;">
          <span style="color:#fff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Filect</span>
        </td></tr>
        <tr><td style="padding:40px;">
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;">${greeting}</p>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">${opener}</p>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">
            You can fix this in about 30 seconds — click below to update your payment method. Once it's updated we'll retry the charge automatically.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin:28px 0;">
            <tr><td style="background:#7C4DFF;border-radius:8px;padding:14px 32px;">
              <a href="${portalUrl}" style="color:#fff;text-decoration:none;font-size:16px;font-weight:600;">Update payment method →</a>
            </td></tr>
          </table>
          <p style="margin:0;font-size:14px;color:#666;line-height:1.6;">
            Questions? Just reply to this email and we'll help you out.
          </p>
          <p style="margin:24px 0 0;font-size:14px;color:#1a1a2e;">— The Filect team</p>
        </td></tr>
        <tr><td style="padding:20px 40px;border-top:1px solid #f0f0f0;text-align:center;">
          <p style="margin:0;font-size:12px;color:#999;">© 2026 Filect. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>`,
  };
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const body = await req.json().catch(() => ({}));
    const userId: string | undefined = body.user_id;
    const kind: "first" | "reminder" = body.kind === "reminder" ? "reminder" : "first";
    if (!userId) {
      return new Response(JSON.stringify({ ok: false, error: "missing user_id" }), { status: 400, headers: cors });
    }

    // Look up the subscription row (latest first).
    const { data: sub, error: subErr } = await supabase
      .from("subscriptions")
      .select("user_id, stripe_customer_id, status, payment_failed_email_sent_at, payment_failed_reminder_sent_at")
      .eq("user_id", userId)
      .not("stripe_customer_id", "is", null)
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (subErr || !sub) {
      return new Response(JSON.stringify({ ok: false, error: "no subscription" }), { status: 404, headers: cors });
    }

    // Idempotency: skip if we already sent THIS kind within the last 24h.
    const stampCol = kind === "first" ? "payment_failed_email_sent_at" : "payment_failed_reminder_sent_at";
    const lastSent = sub[stampCol] ? new Date(sub[stampCol]).getTime() : 0;
    if (lastSent && Date.now() - lastSent < 24 * 60 * 60 * 1000) {
      return new Response(JSON.stringify({ ok: true, skipped: "already sent in last 24h" }), { headers: cors });
    }

    // Get the user's email + name from auth.users.
    const { data: userRes, error: userErr } = await supabase.auth.admin.getUserById(userId);
    if (userErr || !userRes?.user?.email) {
      return new Response(JSON.stringify({ ok: false, error: "no user email" }), { status: 404, headers: cors });
    }
    const email = userRes.user.email;
    const fullName = (userRes.user.user_metadata?.full_name as string | undefined) || null;

    // Build a deep link that opens the Stripe portal for this user.
    // create-portal-session redirects, so the email link works one-tap.
    const portalUrl = `${SUPABASE_URL}/functions/v1/create-portal-session?user_id=${encodeURIComponent(userId)}`;

    const { subject, html } = buildEmail(fullName, portalUrl, kind === "reminder");

    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: FROM, to: email, subject, html }),
    });
    if (!res.ok) {
      const errText = await res.text();
      console.error("resend error:", errText);
      return new Response(JSON.stringify({ ok: false, error: "send_failed", detail: errText }), { status: 502, headers: cors });
    }

    // Mark as sent so we don't double-send.
    await supabase
      .from("subscriptions")
      .update({ [stampCol]: new Date().toISOString() })
      .eq("user_id", userId)
      .eq("stripe_customer_id", sub.stripe_customer_id);

    return new Response(JSON.stringify({ ok: true, sent_to: email, kind }), { headers: cors });
  } catch (e) {
    console.error("send-payment-failed-email error:", e);
    return new Response(JSON.stringify({ ok: false, error: "server", detail: String(e) }), { status: 500, headers: cors });
  }
});
