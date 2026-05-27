import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

// Switches an existing subscription to a new price (upgrade / downgrade / change
// billing period) by modifying the subscription item in place, with proration.
// This is NOT a new checkout — it keeps the same subscription (and trial).
serve(async (req) => {
  try {
    const url = new URL(req.url);
    const userId = url.searchParams.get("user_id");
    const priceId = url.searchParams.get("price_id");
    if (!userId || !priceId) return new Response("Missing user_id or price_id", { status: 400 });

    const { data } = await supabase
      .from("subscriptions")
      .select("stripe_subscription_id")
      .eq("user_id", userId)
      .in("status", ["active", "trialing"])
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    // No active sub to change — send them to start one.
    if (!data?.stripe_subscription_id) {
      return Response.redirect("https://filect.io/pricing.html", 303);
    }

    const sub = await stripe.subscriptions.retrieve(data.stripe_subscription_id);
    const itemId = sub.items?.data?.[0]?.id;
    if (!itemId) return new Response("Subscription has no item", { status: 400 });

    const updated = await stripe.subscriptions.update(sub.id, {
      items: [{ id: itemId, price: priceId }],
      proration_behavior: "create_prorations",
      // Switching to a plan implies they want to keep the subscription, so clear
      // any pending cancellation.
      cancel_at_period_end: false,
    });

    const item = updated.items?.data?.[0];
    await supabase.from("subscriptions").update({
      price_id: item?.price?.id ?? priceId,
      status: updated.status,
      cancel_at_period_end: false,
      updated_at: new Date().toISOString(),
    }).eq("stripe_subscription_id", updated.id);

    return Response.redirect("https://filect.io/account?changed=1", 303);
  } catch (e) {
    console.error("change-plan error:", e);
    return new Response("Change plan error", { status: 500 });
  }
});
