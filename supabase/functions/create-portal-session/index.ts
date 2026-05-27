import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

// Opens Stripe's hosted Customer Portal so a logged-in user can manage or cancel
// their subscription and update payment details. Looks up the customer id we
// stored at provisioning time (subscriptions.stripe_customer_id) from user_id.
serve(async (req) => {
  try {
    const url = new URL(req.url);
    const userId = url.searchParams.get("user_id");
    if (!userId) return new Response("Missing user_id", { status: 400 });

    const { data, error } = await supabase
      .from("subscriptions")
      .select("stripe_customer_id")
      .eq("user_id", userId)
      .not("stripe_customer_id", "is", null)
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    if (error || !data?.stripe_customer_id) {
      return new Response("No billing account found", { status: 404 });
    }

    const session = await stripe.billingPortal.sessions.create({
      customer: data.stripe_customer_id,
      // Portal config with plan switching (upgrade/downgrade) enabled.
      configuration: "bpc_1TbSmABATYQXewwiMDPLw03T",
      return_url: "https://filect.io/account",
    });

    return Response.redirect(session.url, 303);
  } catch (e) {
    console.error("create-portal-session error:", e);
    return new Response("Portal error", { status: 500 });
  }
});
