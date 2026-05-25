import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Stripe from "https://esm.sh/stripe@14";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, { apiVersion: "2023-10-16" });
const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET")!;

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
);

serve(async (req) => {
  const body = await req.text();
  const sig = req.headers.get("stripe-signature")!;

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig, webhookSecret);
  } catch (err) {
    return new Response(`Webhook error: ${err.message}`, { status: 400 });
  }

  // CAPTURE: a checkout session the user started but never completed.
  // Stripe fires this when a session expires (~24h after creation). We backdate
  // created_at to the session's original creation time so the recovery cadence
  // (24h nudge / 72h discount) is measured from when they actually abandoned.
  if (event.type === "checkout.session.expired") {
    const session = event.data.object as Stripe.Checkout.Session;
    const email = session.customer_details?.email ?? session.customer_email;
    if (!email) return new Response("ok", { status: 200 });

    await supabase.from("abandoned_checkouts").upsert({
      stripe_session_id: session.id,
      stripe_customer_id:
        typeof session.customer === "string" ? session.customer : null,
      user_id: session.metadata?.user_id ?? null,
      email,
      name: session.customer_details?.name ?? null,
      created_at: new Date(session.created * 1000).toISOString(),
    }, { onConflict: "stripe_session_id", ignoreDuplicates: true });
  }

  // CONVERT: a completed checkout. Match by email so any prior abandoned rows
  // for this person are flagged converted and drop out of the recovery sequence.
  if (event.type === "checkout.session.completed") {
    const session = event.data.object as Stripe.Checkout.Session;
    const email = session.customer_details?.email ?? session.customer_email;
    const customerId =
      typeof session.customer === "string" ? session.customer : session.customer?.id;

    if (email) {
      await supabase
        .from("abandoned_checkouts")
        .update({ converted_at: new Date().toISOString() })
        .eq("email", email)
        .is("converted_at", null);
    } else if (customerId) {
      await supabase
        .from("abandoned_checkouts")
        .update({ converted_at: new Date().toISOString() })
        .eq("stripe_customer_id", customerId)
        .is("converted_at", null);
    }
  }

  // CONVERT (fallback): subscription created — match by customer id.
  if (event.type === "customer.subscription.created") {
    const sub = event.data.object as Stripe.Subscription;
    const customerId =
      typeof sub.customer === "string" ? sub.customer : sub.customer?.id;

    if (customerId) {
      await supabase
        .from("abandoned_checkouts")
        .update({ converted_at: new Date().toISOString() })
        .eq("stripe_customer_id", customerId)
        .is("converted_at", null);
    }
  }

  return new Response("ok", { status: 200 });
});
