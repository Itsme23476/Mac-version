import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Reminder for people who CREATED AN ACCOUNT but never reached Stripe checkout
// (no subscription, and not already in abandoned_checkouts — those get the
// separate recovery sequence). Nudges them to pick a plan. One email per
// person, ever (tracked in public.signup_reminders).

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const FROM = "Filect <team@filect.io>";
const SUBJECT = "You're almost set up — pick your Filect plan";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

async function sendEmail(to: string, subject: string, html: string): Promise<boolean> {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ from: FROM, to, subject, html }),
  });
  return res.ok;
}

function reminderEmail(name: string | null, url: string): string {
  const greeting = name ? `Hi ${name.split(" ")[0]},` : "Hi there,";
  return `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:40px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
        <tr><td style="background:#7C4DFF;padding:32px 40px;text-align:center;">
          <span style="color:#fff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Filect</span>
        </td></tr>
        <tr><td style="padding:40px;">
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;">${greeting}</p>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">
            Thanks for creating your Filect account! You haven't picked a plan yet —
            and you're just one step away from letting Filect organize your files automatically.
          </p>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">
            Every plan starts with a <strong>10-day free trial</strong>. You won't be
            charged until it ends, and you can cancel anytime before then.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin:28px 0;">
            <tr><td style="background:#7C4DFF;border-radius:8px;padding:14px 32px;">
              <a href="${url}" style="color:#fff;text-decoration:none;font-size:16px;font-weight:600;">Choose your plan →</a>
            </td></tr>
          </table>
          <p style="margin:0;font-size:14px;color:#666;line-height:1.6;">
            Questions about which plan fits? Just reply to this email — we're happy to help.
          </p>
          <p style="margin:24px 0 0;font-size:14px;color:#1a1a2e;">— The Filect team</p>
        </td></tr>
        <tr><td style="padding:20px 40px;border-top:1px solid #f0f0f0;text-align:center;">
          <p style="margin:0;font-size:12px;color:#999;">© 2026 Filect. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>`;
}

// Land the recipient on the pricing page already identified, so picking a plan
// goes straight to Stripe checkout (no second login) — same uid/email handoff
// the desktop app uses.
function planUrl(userId: string, email: string): string {
  const p = new URLSearchParams({ uid: userId, email, source: "email" });
  return `https://filect.io/pricing.html?${p.toString()}`;
}

serve(async (req) => {
  let body: Record<string, unknown> = {};
  try { body = await req.json(); } catch (_e) { body = {}; }

  // TEST: send a single sample to a given address; touches no real users/rows.
  if (typeof body.test_email === "string") {
    const ok = await sendEmail(
      body.test_email,
      SUBJECT,
      reminderEmail(null, planUrl("00000000-0000-0000-0000-000000000000", body.test_email)),
    );
    return Response.json({ mode: "test", to: body.test_email, sent: ok });
  }

  const { data: candidates, error } = await supabase.rpc("get_signup_reminder_candidates");
  if (error) return Response.json({ error: error.message }, { status: 500 });

  // DRY RUN: report who would be emailed, send nothing.
  if (body.dry_run === true) {
    return Response.json({ mode: "dry_run", count: (candidates ?? []).length, candidates });
  }

  let sent = 0;
  for (const c of (candidates ?? []) as Array<{ user_id: string; email: string; full_name: string | null }>) {
    const ok = await sendEmail(c.email, SUBJECT, reminderEmail(c.full_name, planUrl(c.user_id, c.email)));
    if (ok) {
      // Record immediately so a retry/overlap can never double-send.
      await supabase.from("signup_reminders").insert({ user_id: c.user_id, email: c.email });
      sent++;
    }
  }
  return Response.json({ mode: "live", candidates: (candidates ?? []).length, sent });
});
