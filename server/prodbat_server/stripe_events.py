"""Turns a Stripe webhook event, forwarded by the site's own handler,
into payments. The site has already checked Stripe's signature; we trust
it because it arrives with the project's secret key."""

import datetime as dt
import re


def product_name(description: str | None) -> str:
    """'1 × Pro plan (at $19.00 / month)' -> 'Pro plan'"""
    name = re.sub(r"^\d+\s*[×x]\s*", "", (description or "").strip())
    name = re.sub(r"\s*\((at|with)\s[^)]*\)\s*$", "", name)
    return name[:200] or "Payment"


def payments_from_event(event: dict) -> list[dict]:
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    created = dt.datetime.fromtimestamp(event.get("created") or obj.get("created") or 0, dt.UTC)

    # Subscriptions and invoices. Both event names fire for one invoice;
    # the invoice id keeps it to one payment.
    if kind in ("invoice.paid", "invoice.payment_succeeded"):
        amount = obj.get("amount_paid") or 0
        if amount <= 0:
            return []
        lines = (obj.get("lines") or {}).get("data") or []
        product = product_name(lines[0].get("description")) if lines else "Payment"
        if len(lines) > 1:
            product += f" and {len(lines) - 1} more"
        paid_at = (obj.get("status_transitions") or {}).get("paid_at")
        return [{"stripe_id": obj["id"], "amount": amount, "currency": obj.get("currency", "usd"),
                 "product": product, "ts": dt.datetime.fromtimestamp(paid_at, dt.UTC) if paid_at else created,
                 "ref": None, "subscription": invoice_subscription(obj)}]

    # One-off payments. A subscription checkout also produces an invoice,
    # which is counted above, so only "payment" mode is counted here.
    if kind == "checkout.session.completed":
        if obj.get("mode") != "payment" or obj.get("payment_status") != "paid" or not obj.get("amount_total"):
            return []
        metadata = obj.get("metadata") or {}
        return [{"stripe_id": obj["id"], "amount": obj["amount_total"], "currency": obj.get("currency", "usd"),
                 "product": (metadata.get("product") or metadata.get("product_name") or "One-off payment")[:200],
                 "ts": created, "ref": obj.get("client_reference_id"), "subscription": None}]
    return []


def invoice_subscription(invoice: dict) -> str | None:
    """The subscription an invoice belongs to, in older and newer Stripe API versions."""
    sub = invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if not sub:
        details = ((invoice.get("parent") or {}).get("subscription_details") or {})
        sub = details.get("subscription")
    return str(sub)[:100] if sub else None


def subscription_checkout(event: dict) -> tuple[str, str] | None:
    """(subscription id, page-load reference) when a subscription checkout
    carries the reference the Prodbat script handed out."""
    if event.get("type") != "checkout.session.completed":
        return None
    obj = (event.get("data") or {}).get("object") or {}
    sub, ref = obj.get("subscription"), obj.get("client_reference_id")
    if obj.get("mode") != "subscription" or not sub or not ref:
        return None
    return str(sub)[:100], str(ref)[:64]
