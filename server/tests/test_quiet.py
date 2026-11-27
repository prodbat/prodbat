import re
import datetime as dt

from sqlalchemy import select, text

from prodbat_server.models import Incident, Payment, Pageview

from prodbat_server.traffic import floor_hour, roll_up

from .helpers import NOW, emails, evaluate, run


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


def learn(db, project):
    """What the worker does every 10 minutes, over all the history in the test."""
    async def go(sessions):
        async with sessions() as s:
            await roll_up(s, project.id, NOW, since=NOW - dt.timedelta(days=60))
            await s.commit()

    run(go, db)


def test_with_its_hourly_pattern_learned_a_small_site_s_night_is_still_fine(db, project):
    db.add_all(small_site(project, 28, NOW - dt.timedelta(hours=14)))
    learn(db, project)
    evaluate(db)
    assert "visitors" not in open_quiet(db)

    db.query(Pageview).delete()
    db.execute(text("delete from visit_hours"))
    db.add_all(small_site(project, 28, NOW - dt.timedelta(days=3)))
    learn(db, project)
    evaluate(db)
    incident = open_quiet(db)["visitors"]
    assert "At these hours of the week you normally get about" in incident.what


def test_a_site_that_goes_dark_in_its_busy_hours_is_broken_within_minutes(db, project):
    # A visit a minute for the 12 whole hours before this hour of the day and 6 after, for a week; nothing
    # at night. Whole hours, since each hour's rate is an average: a site that stopped dead every day halfway
    # through an hour would have had a long silence at the start of each night, by that average.
    views = []
    for d in range(1, 8):
        around = floor_hour(NOW) - dt.timedelta(days=d)
        views += [view_at(project, f"v{d}-{m}", around + dt.timedelta(minutes=m)) for m in range(-720, 360)]
    views += [view_at(project, f"today-{m}", NOW + dt.timedelta(minutes=m))
              for m in range(-720 - NOW.minute, -20)]
    db.add_all(views)
    learn(db, project)
    evaluate(db)
    incident = open_quiet(db)["visitors"]
    assert incident.level == "broken"
    assert re.search(r"The last visit was counted 2\d minutes ago\. At these hours of the week you normally get "
                     r"about (1[5-9]|2\d) visits in that time\.", incident.what)  # depends on the minute it runs


def view_at(project, visitor, ts):
    return Pageview(project_id=project.id, visitor=visitor, path="/", source=None, source_kind="direct", ts=ts)


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
