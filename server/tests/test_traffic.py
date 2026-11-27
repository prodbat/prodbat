import datetime as dt

from sqlalchemy import select

from prodbat_server import traffic
from prodbat_server.models import Pageview, VisitHour
from prodbat_server.traffic import floor_hour, level_for, profile, replay, silence_chance

from .helpers import NOW, run
from .test_quiet import small_site

MONDAY = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)


def view(project, visitor, ts):
    return Pageview(project_id=project.id, visitor=visitor, path="/", source=None, source_kind="direct", ts=ts)


def test_each_hour_counts_visits_not_page_views_and_can_be_counted_again(db, project):
    hour = floor_hour(NOW) - dt.timedelta(hours=3)
    at = lambda minutes: hour + dt.timedelta(minutes=minutes)  # noqa: E731
    db.add_all([view(project, "a", at(5)), view(project, "a", at(10)), view(project, "a", at(50)),  # away 40 minutes
                view(project, "b", at(20)), view(project, "b", at(75)),  # back in the next hour
                view(project, "c", NOW)])  # this hour is not over, so it is not counted yet
    for _ in range(2):
        run(lambda sessions: traffic.roll_up_all(sessions, NOW), db)
        assert {r.hour: r.visits for r in db.scalars(select(VisitHour))} == {hour: 3, hour + traffic.HOUR: 1}


def daily(hours: dict[int, int], days: int, end: dt.datetime = MONDAY + dt.timedelta(days=28)):
    """The same visits at the same hours (UTC) every day for `days` days up to `end`."""
    start = end - dt.timedelta(days=days)
    return {start + dt.timedelta(days=d, hours=h): n for d in range(days) for h, n in hours.items()}, start, end


def test_it_learns_a_small_site_sleeps_at_night():
    counts, first, end = daily({9: 1, 13: 1, 17: 1}, 28)
    rates = profile(counts, first, end)
    assert rates[13] > 0.9 and rates[3] < 0.05  # Monday 13:00 against Monday 03:00
    # Quiet from 17:10 until 07:00 the next morning: nothing unusual.
    last = end + dt.timedelta(hours=17, minutes=10)
    assert level_for(rates, last, last + dt.timedelta(hours=14)) is None
    # Three days with no visit at all is.
    assert level_for(rates, last, last + dt.timedelta(days=3)) is not None


def test_a_busy_site_going_dark_at_its_busiest_is_seen_within_minutes():
    counts, first, end = daily({h: 40 for h in range(12, 19)}, 28)
    rates = profile(counts, first, end)
    noon = end + dt.timedelta(hours=13)
    assert level_for(rates, noon, noon + dt.timedelta(minutes=5)) is None  # under MIN_SILENCE
    assert level_for(rates, noon, noon + dt.timedelta(minutes=15)) == "look"
    assert level_for(rates, noon, noon + dt.timedelta(minutes=30)) == "broken"
    # The same 30 minutes at 03:00, when nobody comes anyway, is nothing.
    night = end + dt.timedelta(hours=3)
    assert level_for(rates, night, night + dt.timedelta(minutes=30)) is None


def test_a_day_of_history_is_enough_to_start():
    counts, first, end = daily({9: 1, 13: 1, 17: 1}, 1)
    assert profile(counts, first, end) is not None
    assert profile(counts, first + dt.timedelta(hours=4), end) is None  # 20 hours is not


def test_a_busy_site_s_first_day_already_shows_its_nights():
    counts, first, end = daily({**{h: 2 for h in range(24)}, **{h: 100 for h in range(9, 21)}}, 1)
    rates = profile(counts, first, end)
    night = end + dt.timedelta(hours=3)
    assert rates[3] < 4
    assert level_for(rates, night, night + dt.timedelta(minutes=40)) is None
    noon = end + dt.timedelta(hours=13)
    assert level_for(rates, noon, noon + dt.timedelta(minutes=15)) == "broken"


def test_with_little_history_an_hour_leans_on_the_site_s_overall_rate():
    counts, first, end = daily({13: 6}, 1)  # six visits a day, all at 13:00
    rates = profile(counts, first, end)
    overall = 6 / 24
    assert rates[3] > 0 and rates[3] < overall  # some night traffic is still possible...
    assert rates[13] > overall  # ...but the one day already shows its shape
    four_weeks = profile(*daily({13: 6}, 28))
    assert four_weeks[3] < rates[3] and four_weeks[13] > rates[13]


def test_one_spike_does_not_make_every_later_week_look_slow():
    counts, first, end = daily({h: 1 for h in range(24)}, 28)
    counts[MONDAY + dt.timedelta(days=7, hours=13)] = 500  # one launch day
    rates = profile(counts, first, end)
    assert 0.9 < rates[13] < 1.1


def test_a_past_outage_does_not_teach_that_silence_is_normal():
    counts, first, end = daily({h: 10 for h in range(24)}, 28)
    outage = {MONDAY + dt.timedelta(days=21, hours=h) for h in range(24)}
    for hour in outage:
        del counts[hour]
    assert profile(counts, first, end, outage)[5] > profile(counts, first, end)[5]


def test_expected_visits_add_up_across_part_hours():
    rates = [0.0] * 168
    rates[13] = 60.0
    chance, usual = silence_chance(rates, MONDAY + dt.timedelta(hours=12, minutes=50),
                                   MONDAY + dt.timedelta(hours=13, minutes=30))
    assert usual == 30.0


def test_replay_shows_what_each_rule_would_have_said_about_a_quiet_night(db, project):
    db.add_all(small_site(project, 40, NOW - dt.timedelta(hours=14)))
    db.commit()

    async def go(sessions):
        async with sessions() as s:
            return await replay(s, project, NOW - dt.timedelta(days=2), NOW)

    result = run(go, db)
    page_views, visits, hourly = result.rules.values()
    assert len(page_views) >= 1  # the rule before 23 Nov called a quiet night an outage
    assert visits == [] and hourly == []
    assert result.visits == 6 and round(result.days) == 2


def test_a_bursty_site_must_go_quieter_than_it_ever_has():
    # 40 visits an hour all afternoon, so 15 quiet minutes is unlikely if visits came one at a time...
    counts, first, end = daily({h: 40 for h in range(12, 19)}, 28)
    rates = profile(counts, first, end)
    noon = end + dt.timedelta(hours=13)
    assert level_for(rates, noon, noon + dt.timedelta(minutes=15)) == "look"
    # ...but this site's visits come in bursts, and it has had 20 quiet minutes in an afternoon before.
    past = [(noon - dt.timedelta(days=3), noon - dt.timedelta(days=3, minutes=-20))]
    longest = traffic.longest_silence(rates, past)
    assert round(longest) == 13
    assert level_for(rates, noon, noon + dt.timedelta(minutes=15), longest) is None
    assert level_for(rates, noon, noon + dt.timedelta(minutes=35), longest) == "look"  # past 1.5 times that
    assert level_for(rates, noon, noon + dt.timedelta(minutes=65), longest) == "broken"  # past twice the bar


def test_past_silences_leave_out_a_past_outage(db, project):
    start = floor_hour(NOW) - dt.timedelta(days=2)
    at = lambda hours: start + dt.timedelta(hours=hours)  # noqa: E731
    db.add_all([view(project, "a", at(0)), view(project, "b", at(1)), view(project, "c", at(5)),
                view(project, "d", at(5.1))])
    db.commit()

    async def go(sessions):
        async with sessions() as s:
            return await traffic.past_silences(s, project.id, start, NOW, excluded={at(3)})

    assert run(go, db) == [(at(0), at(1))]  # 1 to 5 covered the outage; 6 minutes is not a silence
