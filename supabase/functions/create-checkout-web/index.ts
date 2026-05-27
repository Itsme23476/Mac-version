import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

// Dedicated checkout for the WEBSITE signup flow. Mirrors the app's create-checkout
// (10-day trial, subscription tied to the account via metadata.user_id) and adds:
//   - allow_promotion_codes ONLY for monthly plans (so FILECT20 works on monthly,
//     not on annual — monthly/annual share a product so this is the clean way).
// The app's own create-checkout is untouched.
serve(async (req) => {
  try {
    const url = new URL(req.url);
    const userId = url.searchParams.get("user_id");
    const email = url.searchParams.get("email") || undefined;
    const priceId = url.searchParams.get("price_id");
    // Where this checkout originated: "web" (default) or "app" (handed off from
    // the desktop app via the pricing page).
    const source = url.searchParams.get("source") === "app" ? "app" : "web";

    if (!priceId || !userId) {
      return new Response("Missing user_id or price_id", { status: 400 });
    }

    // Prevent duplicate subscriptions: if the user already has an active or
    // trialing subscription, don't create a new one — send them to their account
    // to manage / change plan instead (one active subscription per account).
    try {
      const { data: existingActive } = await supabase
        .from("subscriptions")
        .select("id")
        .eq("user_id", userId)
        .in("status", ["active", "trialing"])
        .limit(1)
        .maybeSingle();
      if (existingActive) {
        return Response.redirect("https://filect.io/account", 303);
      }
    } catch (_e) {
      // If the check fails, fall through and let checkout proceed.
    }

    // Promo code field only on monthly plans.
    let allowPromo = false;
    try {
      const price = await stripe.prices.retrieve(priceId);
      allowPromo = price.recurring?.interval === "month";
    } catch (_e) {
      allowPromo = false;
    }

    // The 10-day free trial is for FIRST-TIME subscribers only. If the user has
    // ever had a subscription (any status), they've already used their trial —
    // they get charged immediately, no second trial.
    let hadSubscription = false;
    try {
      const { data } = await supabase
        .from("subscriptions")
        .select("id")
        .eq("user_id", userId)
        .limit(1)
        .maybeSingle();
      hadSubscription = !!data;
    } catch (_e) {
      hadSubscription = false;
    }

    const session = await stripe.checkout.sessions.create({
      mode: "subscription",
      line_items: [{ price: priceId, quantity: 1 }],
      customer_email: email,
      allow_promotion_codes: allowPromo,
      subscription_data: {
        // Omit trial entirely for returning subscribers.
        ...(hadSubscription ? {} : { trial_period_days: 10 }),
        metadata: { user_id: userId, source },
      },
      // source tracks web-vs-app conversions; plan lets abandoned-cart recovery
      // bring the user back to the exact plan they were buying.
      metadata: { user_id: userId, source, plan: priceId },
      // App-originated payments land on a "return to the app" page (they already
      // have the app); web payments get the download page.
      success_url: source === "app"
        ? "https://filect.io/payment-success?from=app"
        : "https://filect.io/payment-success",
      cancel_url: "https://filect.io/pricing.html",
    });

    return Response.redirect(session.url!, 303);
  } catch (e) {
    console.error("create-checkout-web error:", e);
    return new Response("Checkout error", { status: 500 });
  }
});
