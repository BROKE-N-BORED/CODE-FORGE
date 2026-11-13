name: recipe-payments
description: Building-block recipe — add a payments connector STUB to the owner's project: checkout flow, webhook handler, and order records wired against a provider's TEST mode, going live only at the owner's explicit word. Use when the owner asks for payments, checkout, charging, or selling in a project being built.

# Recipe: Payments — connector stub, test mode first (C10)

A named scaffold, deliberately a **stub**: Forge wires the
project's side of payments completely — checkout entry, webhook
handler, order records — against the provider's TEST/sandbox
mode. Going live is a separate, owner-only decision with real
keys, never something a build does on its own. Card data never
touches the project's own code or database: the provider's
hosted checkout / payment element collects it, the project only
ever sees tokens, statuses, and amounts.

## Before building — pin these with the owner

- Provider (Stripe by default — Forge already has a Stripe
  connector for the owner's own business use; the project still
  gets its own keys, entered through the vault, never in code).
- What is sold: one-off items, subscriptions, or both.
- Currency and where orders must be recorded.

## The scaffold

1. **Config** — provider keys read from environment / the
   project's secrets store only (`PAYMENTS_SECRET_KEY`,
   `PAYMENTS_WEBHOOK_SECRET`); a missing key stops the flow
   with a plain message, never a crash or a silent skip.
2. **Orders first** — an `orders` table (recipe-database shape):
   id, item, amount_cents, currency, status
   (created / paid / failed / refunded), provider reference,
   created/updated. An order exists BEFORE checkout starts;
   money state is never inferred from anything else.
3. **Checkout** — one function creating a provider checkout
   session for an order and returning its URL; the project's
   page/flow sends the buyer there. Success/cancel land back
   on project pages that read the ORDER's status, not the
   return URL's claims.
4. **Webhook** — one endpoint verifying the provider's
   signature before trusting the body; on a verified
   paid/failed/refunded event it updates the matching order
   and logs the event id; duplicate events are idempotent
   (same event id twice = no second state change).
5. **Test mode** — everything above runs on test keys and the
   provider's test card numbers until the owner says "go live"
   in as many words.

## Verify (not done until these pass, all in test mode)

- A test checkout completes and the order flips to paid via
  the WEBHOOK, not the success page.
- A failed test payment leaves the order failed, with the
  reason recorded.
- A webhook with a bad signature is refused.
- Replaying the same webhook changes nothing twice.
- No key, card number, or full webhook payload sits in the
  project's files or logs (grep to prove it).

## Never

No live keys in a build, a test, or a commit — ever. No card
data through project code. No marking an order paid from a
browser redirect. No go-live inside this recipe: that is the
owner's separate, explicit call.
