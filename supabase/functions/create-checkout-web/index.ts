import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });

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

    if (!priceId || !userId) {
      return new Response("Missing user_id or price_id", { status: 400 });
    }

    // Promo code field only on monthly plans.
    let allowPromo = false;
    try {
      const price = await stripe.prices.retrieve(priceId);
      allowPromo = price.recurring?.interval === "month";
    } catch (_e) {
      allowPromo = false;
    }

    const session = await stripe.checkout.sessions.create({
      mode: "subscription",
      line_items: [{ price: priceId, quantity: 1 }],
      customer_email: email,
      allow_promotion_codes: allowPromo,
      subscription_data: {
        trial_period_days: 10,
        metadata: { user_id: userId, source: "web" },
      },
      // source=web lets us track web-vs-app conversions; plan lets abandoned-cart
      // recovery bring the user back to the exact plan they were buying.
      metadata: { user_id: userId, source: "web", plan: priceId },
      success_url: "https://filect.io/payment-success",
      cancel_url: "https://filect.io/pricing.html",
    });

    return Response.redirect(session.url!, 303);
  } catch (e) {
    console.error("create-checkout-web error:", e);
    return new Response("Checkout error", { status: 500 });
  }
});
