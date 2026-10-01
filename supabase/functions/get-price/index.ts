import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import Stripe from "https://esm.sh/stripe@14";

// Tiny public read-only endpoint: returns the display info for a Stripe price.
// Used by /trial-blocked so the page never shows a stale hardcoded price.

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Content-Type": "application/json",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const url = new URL(req.url);
    const priceId = url.searchParams.get("price_id");
    if (!priceId) return new Response(JSON.stringify({ error: "missing price_id" }), { status: 400, headers: cors });

    const price = await stripe.prices.retrieve(priceId, { expand: ["product"] });
    const amount = (price.unit_amount ?? 0) / 100;
    const currency = (price.currency ?? "usd").toUpperCase();
    const interval = price.recurring?.interval ?? "one-time";
    const product = price.product as Stripe.Product;
    // Derive a clean name: nickname > product name > "Subscription"
    const name = price.nickname || product?.name || "Subscription";
    return new Response(JSON.stringify({
      id: price.id,
      name,
      amount,
      currency,
      interval,
      display: `$${amount}/${interval === "year" ? "year" : "month"}`,
    }), { headers: cors });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: cors });
  }
});
