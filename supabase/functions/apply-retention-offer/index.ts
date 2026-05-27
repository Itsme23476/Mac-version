import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

// One-time "stay" offer: 50% off the next month.
const RETENTION_COUPON = "Y4QNbryP";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Content-Type": "application/json",
};

// Applies the retention coupon to the user's subscription and clears any pending
// cancellation. Allowed only once per customer (retention_offer_used guard).
serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const { user_id, reason } = await req.json();
    if (!user_id) return new Response(JSON.stringify({ ok: false, error: "missing_user" }), { status: 400, headers: cors });

    const { data } = await supabase
      .from("subscriptions")
      .select("stripe_subscription_id, retention_offer_used")
      .eq("user_id", user_id)
      .in("status", ["active", "trialing"])
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    if (!data?.stripe_subscription_id) {
      return new Response(JSON.stringify({ ok: false, error: "no_subscription" }), { status: 404, headers: cors });
    }
    if (data.retention_offer_used) {
      return new Response(JSON.stringify({ ok: false, error: "already_used" }), { status: 409, headers: cors });
    }

    const updated = await stripe.subscriptions.update(data.stripe_subscription_id, {
      coupon: RETENTION_COUPON,
      cancel_at_period_end: false,
    });

    await supabase.from("subscriptions").update({
      retention_offer_used: true,   // once-per-customer guard (permanent)
      discount_pending: true,       // drives the "50% off next invoice" note; cleared by webhook
      cancel_at_period_end: false,
      updated_at: new Date().toISOString(),
    }).eq("stripe_subscription_id", updated.id);

    // Capture the churn reason even though they stayed — it's still feedback.
    if (reason && reason.trim().length) {
      await supabase.from("cancellation_feedback").insert({
        user_id,
        stripe_subscription_id: updated.id,
        reason: reason.trim(),
        outcome: "stayed_50off",
      });
    }

    return new Response(JSON.stringify({ ok: true }), { headers: cors });
  } catch (e) {
    console.error("apply-retention-offer error:", e);
    return new Response(JSON.stringify({ ok: false, error: "server" }), { status: 500, headers: cors });
  }
});
