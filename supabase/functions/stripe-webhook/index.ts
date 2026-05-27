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
      // The plan they were buying, so the recovery email can bring them back
      // to the exact plan (set by create-checkout-web in session metadata).
      plan: session.metadata?.plan ?? null,
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

    // PROVISION: write the subscription into the DB so the app recognizes the
    // user as subscribed (incl. which plan). user_id comes from the checkout
    // session metadata set by create-checkout. Idempotent upsert on sub id.
    const userId = session.metadata?.user_id;
    const subId =
      typeof session.subscription === "string" ? session.subscription : session.subscription?.id;
    if (userId && subId) {
      try {
        const sub = await stripe.subscriptions.retrieve(subId);
        // Stripe's newer API exposes current_period_* on the subscription ITEM,
        // not the top level — read item first, fall back to top level, and guard
        // against undefined so the upsert never throws (which would drop the row).
        const item = sub.items?.data?.[0];
        const startTs = item?.current_period_start ?? sub.current_period_start;
        const endTs = item?.current_period_end ?? sub.current_period_end;
        const { error: subErr } = await supabase.from("subscriptions").upsert({
          user_id: userId,
          stripe_customer_id: customerId ?? null,
          stripe_subscription_id: subId,
          status: sub.status,
          price_id: item?.price?.id ?? null,
          // Where the paying customer came from. create-checkout-web sets
          // metadata.source='web'; the app's create-checkout omits it → 'app'.
          source: session.metadata?.source ?? "app",
          current_period_start: startTs ? new Date(startTs * 1000).toISOString() : null,
          current_period_end: endTs ? new Date(endTs * 1000).toISOString() : null,
          updated_at: new Date().toISOString(),
        }, { onConflict: "stripe_subscription_id" });
        if (subErr) console.error("subscription upsert error:", subErr);
      } catch (e) {
        console.error("subscription provision error:", e);
      }
    }

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

  // A paid invoice consumes the one-time retention discount, so clear the
  // "50% off next invoice" flag once it's been used up.
  if (event.type === "invoice.payment_succeeded") {
    const invoice = event.data.object as Stripe.Invoice;
    const subId = typeof invoice.subscription === "string"
      ? invoice.subscription
      : invoice.subscription?.id;
    if (subId) {
      await supabase
        .from("subscriptions")
        .update({ discount_pending: false, updated_at: new Date().toISOString() })
        .eq("stripe_subscription_id", subId)
        .eq("discount_pending", true);
    }
  }

  // SYNC: keep our subscriptions row mirrored to Stripe on every renewal,
  // plan change, or cancellation — so status / period / cancel flag never go
  // stale (the app reads these to decide access).
  if (event.type === "customer.subscription.updated" || event.type === "customer.subscription.deleted") {
    const sub = event.data.object as Stripe.Subscription;
    const item = sub.items?.data?.[0];
    const startTs = item?.current_period_start ?? sub.current_period_start;
    const endTs = item?.current_period_end ?? sub.current_period_end;
    const customerId = typeof sub.customer === "string" ? sub.customer : sub.customer?.id;
    const fields = {
      status: sub.status,
      price_id: item?.price?.id ?? null,
      current_period_start: startTs ? new Date(startTs * 1000).toISOString() : null,
      current_period_end: endTs ? new Date(endTs * 1000).toISOString() : null,
      cancel_at_period_end: sub.cancel_at_period_end ?? false,
      updated_at: new Date().toISOString(),
    };

    const { data: existing } = await supabase
      .from("subscriptions")
      .select("id")
      .eq("stripe_subscription_id", sub.id)
      .maybeSingle();

    if (existing) {
      // Row exists — just refresh it.
      await supabase.from("subscriptions").update(fields).eq("stripe_subscription_id", sub.id);
    } else if (sub.metadata?.user_id) {
      // New sub we haven't recorded yet but we know the user — insert it.
      await supabase.from("subscriptions").upsert({
        user_id: sub.metadata.user_id,
        stripe_customer_id: customerId ?? null,
        stripe_subscription_id: sub.id,
        source: sub.metadata?.source ?? "app",
        ...fields,
      }, { onConflict: "stripe_subscription_id" });
    }
    // (Legacy subs with no row and no metadata.user_id are handled by the
    // one-time email-based backfill, not here.)
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
