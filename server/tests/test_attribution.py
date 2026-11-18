from sqlalchemy import select

from prodbat_server.models import Attribution, Payment
from prodbat_server.stripe_events import invoice_subscription

from .helpers import NOW, page_text, view
from .test_api import invoice
from .test_explore import text_of


def renewal(id, sub, newer_api=False):
    event = invoice(id)
    obj = event["data"]["object"]
    if newer_api:
        obj["parent"] = {"subscription_details": {"subscription": sub}}
    else:
        obj["subscription"] = sub
    return event


def subscription_checkout(sub, ref):
    return {"type": "checkout.session.completed", "created": int(NOW.timestamp()) - 120, "data": {"object": {
        "id": f"cs_{sub}", "mode": "subscription", "payment_status": "paid", "amount_total": 1900, "currency": "usd",
        "subscription": sub, "client_reference_id": ref}}}


def test_both_stripe_api_versions_name_the_subscription():
    assert invoice_subscription({"subscription": "sub_1"}) == "sub_1"
    assert invoice_subscription({"subscription": {"id": "sub_2"}}) == "sub_2"
    assert invoice_subscription({"parent": {"subscription_details": {"subscription": "sub_3"}}}) == "sub_3"
    assert invoice_subscription({}) is None


def test_every_renewal_of_a_subscription_is_credited_to_where_the_buyer_came_from(client, db, project, key):
    db.add_all([view(project, "buyer", "/", "ChatGPT", "ai"), view(project, "buyer", "/pricing", None, "internal", ref="r1")])
    db.commit()
    # The first invoice can arrive before the checkout event: it is credited once the checkout says where from.
    client.post("/api/stripe", json=renewal("in_1", "sub_A"), headers=key)
    assert db.scalar(select(Payment.source)) is None
    client.post("/api/stripe", json=subscription_checkout("sub_A", "r1"), headers=key)
    db.expire_all()
    assert db.scalar(select(Payment.source).where(Payment.stripe_id == "in_1")) == "ChatGPT"
    assert db.get(Attribution, (project.id, "sub_A")).source == "ChatGPT"

    # Later renewals, in the newer API's shape, are credited at once.
    client.post("/api/stripe", json=renewal("in_2", "sub_A", newer_api=True), headers=key)
    assert db.scalar(select(Payment.source).where(Payment.stripe_id == "in_2")) == "ChatGPT"
    # A subscription nobody traced stays untraced.
    client.post("/api/stripe", json=renewal("in_3", "sub_B"), headers=key)
    assert db.scalar(select(Payment.source).where(Payment.stripe_id == "in_3")) is None

    page = page_text(client, project)
    assert "Where buyers came from this week ChatGPT 2 payments $38" in page
    metrics = text_of(client, f"/d/{project.page_token}/metrics", range="7d")
    assert "Revenue by source" in metrics and "ChatGPT 2 $38" in metrics and "Not traced 1 $19" in metrics


def test_a_plan_without_attribution_does_not_show_it(client, db, project, key):
    project.attribution_enabled = False
    db.add(view(project, "buyer", "/", "Google", "search", ref="r2"))
    db.commit()
    client.post("/api/stripe", json=renewal("in_9", "sub_C"), headers=key)
    client.post("/api/stripe", json=subscription_checkout("sub_C", "r2"), headers=key)
    assert "Where buyers came from" not in page_text(client, project)
    assert "Revenue by source" not in text_of(client, f"/d/{project.page_token}/metrics")
