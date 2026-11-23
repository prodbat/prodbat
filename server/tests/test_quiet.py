import datetime as dt

from sqlalchemy import select

from prodbat_server.models import Incident, Payment, Pageview

from .helpers import NOW, emails, evaluate


def payments(project, every_hours: float, count: int, last: dt.datetime):
    return [Payment(project_id=project.id, stripe_id=f"pi_{i}", amount=1900, currency="usd", product="Pro",
                    ts=last - dt.timedelta(hours=every_hours * i)) for i in range(count)]


def open_quiet(db):
    db.expire_all()
    return {i.key: i for i in db.scalars(select(Incident).where(Incident.kind == "quiet", Incident.ended_at.is_(None)))}


def test_payments_that_stop_for_much_longer_than_usual_need_a_look_then_are_broken(db, project):
    # About two payments a day for two weeks, the last one 2 days ago: unusual, but not yet past the bar.
    db.add_all(payments(project, 12, 28, NOW - dt.timedelta(days=2)))
    db.commit()
    evaluate(db)
    assert "payments" not in open_quiet(db)

    db.query(Payment).delete()
    db.add_all(payments(project, 12, 28, NOW - dt.timedelta(days=4)))
    db.commit()
    evaluate(db)
    incident = open_quiet(db)["payments"]
    assert incident.level == "look" and incident.title == "Payments have stopped"
    assert "The last payment came in 4 days ago. You normally get one about every 12 hours." in incident.what
    assert emails(db)[-1].subject == "Needs a look: Payments have stopped on yourapp.com"

    # Six days without one at two a day is past any chance: broken.
    db.query(Payment).delete()
    db.add_all(payments(project, 12, 28, NOW - dt.timedelta(days=6)))
    db.commit()
    evaluate(db)
    assert open_quiet(db)["payments"].level == "broken"
    assert emails(db)[-1].subject == "Broken: Payments have stopped on yourapp.com"

    # The next payment closes it.
    db.add(Payment(project_id=project.id, stripe_id="pi_new", amount=1900, currency="usd", product="Pro", ts=NOW))
    db.commit()
    evaluate(db)
    assert "payments" not in open_quiet(db)
    assert emails(db)[-1].subject == "Working again: Payments have stopped on yourapp.com"


def test_too_little_history_never_alerts(db, project):
    db.add_all(payments(project, 24, 5, NOW - dt.timedelta(days=20)))  # five payments, long ago
    db.commit()
    evaluate(db)
    assert open_quiet(db) == {}


def small_site(project, days: int, last: dt.datetime):
    """Three visitors a day, four hours apart, each looking at eight pages over a few minutes; the
    last visit starts at `last`, and each night is 16 hours without one."""
    views = []
    for d in range(days):
        for h in (0, 4, 8):
            start = last - dt.timedelta(days=d, hours=h)
            views += [Pageview(project_id=project.id, visitor=f"v{d}-{h}", path=f"/p{i}", source=None,
                               source_kind="direct", ts=start + dt.timedelta(minutes=i)) for i in range(8)]
    return views


def test_a_quiet_night_on_a_small_site_is_not_a_problem(db, project):
    # 3 visits a day (24 page views), and nobody for the 14 hours since the last.
    db.add_all(small_site(project, 28, NOW - dt.timedelta(hours=14)))
    db.commit()
    evaluate(db)
    assert "visitors" not in open_quiet(db)

    # Three whole days with no visit at all is still worth a look.
    db.query(Pageview).delete()
    db.add_all(small_site(project, 28, NOW - dt.timedelta(days=3)))
    db.commit()
    evaluate(db)
    incident = open_quiet(db)["visitors"]
    assert incident.level == "look"
    assert "You normally get one about every 8 hours. The longest quiet stretch in the last 4 weeks was 16 hours." \
        in incident.what


def test_visits_that_stop_point_at_the_script_or_the_site(db, project, client, key):
    db.add_all([Pageview(project_id=project.id, visitor=f"v{i}", path="/", source=None, source_kind="direct",
                         ts=NOW - dt.timedelta(hours=8) - dt.timedelta(minutes=10 * i)) for i in range(600)])
    db.commit()
    evaluate(db)
    incident = open_quiet(db)["visitors"]
    assert incident.level == "broken" and "the Prodbat script was removed in a deploy" in incident.what

    task = client.get("/api/tasks/next", headers=key).json()["task"]
    assert task["title"] == "Visitors have stopped being counted" and "npx prodbat verify" in task["text"]
    assert client.post(f"/api/tasks/{task['id']}/check", headers=key).json()["fixed"] is None
