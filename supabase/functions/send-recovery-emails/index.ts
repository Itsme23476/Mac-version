import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const FROM = "Filect <team@filect.io>";
const RECOVERY_BASE = "https://filect.io/open";

// Build a per-recipient recovery link so the landing page can start checkout
// for that exact account (Starter plan) without making them log in again.
function recoveryUrl(row: { user_id?: string | null; email?: string | null }): string {
  const params = new URLSearchParams();
  if (row.user_id) params.set("uid", row.user_id);
  if (row.email) params.set("email", row.email);
  const qs = params.toString();
  return qs ? `${RECOVERY_BASE}?${qs}` : RECOVERY_BASE;
}

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

async function sendEmail(to: string, subject: string, html: string) {
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

function nudgeEmail(name: string | null, url: string): string {
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
            You were so close! You started setting up Filect but didn't quite finish.
          </p>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">
            Filect keeps your files organized automatically — so you spend less time searching and more time getting things done.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin:28px 0;">
            <tr><td style="background:#7C4DFF;border-radius:8px;padding:14px 32px;">
              <a href="${url}" style="color:#fff;text-decoration:none;font-size:16px;font-weight:600;">Complete your setup →</a>
            </td></tr>
          </table>
          <p style="margin:0;font-size:14px;color:#666;line-height:1.6;">
            If you have any questions, just reply to this email — we're happy to help.
          </p>
          <p style="margin:24px 0 0;font-size:14px;color:#1a1a2e;">
            — The Filect team
          </p>
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

function discountEmail(name: string | null, url: string): string {
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
            We noticed you haven't finished setting up Filect yet. We'd love to help you get started — so here's a little push:
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f0ff;border-radius:10px;margin:20px 0;">
            <tr><td style="padding:24px;text-align:center;">
              <p style="margin:0 0 8px;font-size:14px;color:#7C4DFF;font-weight:600;text-transform:uppercase;letter-spacing:1px;">Your exclusive discount</p>
              <p style="margin:0 0 4px;font-size:36px;font-weight:800;color:#1a1a2e;letter-spacing:-1px;">20% off</p>
              <p style="margin:0 0 16px;font-size:14px;color:#555;">your first payment — use code at checkout:</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#7C4DFF;letter-spacing:2px;font-family:monospace;">FILECT20</p>
            </td></tr>
          </table>
          <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;line-height:1.6;">
            This code is just for you and expires in 7 days — don't let it go to waste!
          </p>
          <table cellpadding="0" cellspacing="0" style="margin:28px 0;">
            <tr><td style="background:#7C4DFF;border-radius:8px;padding:14px 32px;">
              <a href="${url}" style="color:#fff;text-decoration:none;font-size:16px;font-weight:600;">Claim your discount →</a>
            </td></tr>
          </table>
          <p style="margin:0;font-size:14px;color:#666;line-height:1.6;">
            Questions? Just reply — we read every email.
          </p>
          <p style="margin:24px 0 0;font-size:14px;color:#1a1a2e;">
            — The Filect team
          </p>
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

serve(async () => {
  const now = new Date();
  const h24ago = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString();
  const h72ago = new Date(now.getTime() - 72 * 60 * 60 * 1000).toISOString();

  // 24h nudge: created > 24h ago, no nudge sent, not converted
  const { data: nudgeCandidates } = await supabase
    .from("abandoned_checkouts")
    .select("*")
    .is("converted_at", null)
    .is("nudge_sent_at", null)
    .lt("created_at", h24ago);

  for (const row of nudgeCandidates ?? []) {
    const ok = await sendEmail(
      row.email,
      "You left something behind 👀",
      nudgeEmail(row.name, recoveryUrl(row))
    );
    if (ok) {
      await supabase
        .from("abandoned_checkouts")
        .update({ nudge_sent_at: now.toISOString() })
        .eq("id", row.id);
    }
  }

  // 72h discount: nudge already sent, created > 72h ago, no discount sent, not converted
  const { data: discountCandidates } = await supabase
    .from("abandoned_checkouts")
    .select("*")
    .is("converted_at", null)
    .not("nudge_sent_at", "is", null)
    .is("discount_sent_at", null)
    .lt("created_at", h72ago);

  for (const row of discountCandidates ?? []) {
    const ok = await sendEmail(
      row.email,
      "20% off — just for you, Filect",
      discountEmail(row.name, recoveryUrl(row))
    );
    if (ok) {
      await supabase
        .from("abandoned_checkouts")
        .update({ discount_sent_at: now.toISOString() })
        .eq("id", row.id);
    }
  }

  return new Response(
    JSON.stringify({
      nudge: nudgeCandidates?.length ?? 0,
      discount: discountCandidates?.length ?? 0,
    }),
    { headers: { "Content-Type": "application/json" } }
  );
});
