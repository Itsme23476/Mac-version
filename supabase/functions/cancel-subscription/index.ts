import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });
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

// Cancels at period end (keeps access until the period ends) and records the
// free-text reason for churn feedback. Reason must be at least 30 chars.
serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const { user_id, reason } = await req.json();
    if (!user_id) return new Response(JSON.stringify({ ok: false, error: "missing_user" }), { status: 400, headers: cors });
    if (!reason || reason.trim().length < 30) {
      return new Response(JSON.stringify({ ok: false, error: "reason_too_short" }), { status: 400, headers: cors });
    }

    const { data } = await supabase
      .from("subscriptions")
      .select("stripe_subscription_id")
      .eq("user_id", user_id)
      .in("status", ["active", "trialing"])
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    if (!data?.stripe_subscription_id) {
      return new Response(JSON.stringify({ ok: false, error: "no_subscription" }), { status: 404, headers: cors });
    }

    const updated = await stripe.subscriptions.update(data.stripe_subscription_id, {
      cancel_at_period_end: true,
    });

    await supabase.from("subscriptions").update({
      cancel_at_period_end: true,
      updated_at: new Date().toISOString(),
    }).eq("stripe_subscription_id", updated.id);

    await supabase.from("cancellation_feedback").insert({
      user_id,
      stripe_subscription_id: updated.id,
      reason: reason.trim(),
      outcome: "canceled",
    });

    const item = updated.items?.data?.[0];
    const endTs = item?.current_period_end ?? updated.current_period_end ?? updated.trial_end;
    return new Response(JSON.stringify({
      ok: true,
      ends: endTs ? new Date(endTs * 1000).toISOString() : null,
    }), { headers: cors });
  } catch (e) {
    console.error("cancel-subscription error:", e);
    return new Response(JSON.stringify({ ok: false, error: "server" }), { status: 500, headers: cors });
  }
});
