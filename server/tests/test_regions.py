import datetime as dt

from sqlalchemy import select

from prodbat_server.checks import run_due_checks
from prodbat_server.models import Incident, SiteCheck
from prodbat_server.probe import probe_once

from .helpers import SETTINGS, http_client, minutes_ago, run

OREGON = SETTINGS.model_copy(update={"region": "oregon"})
FRANKFURT = SETTINGS.model_copy(update={"region": "frankfurt"})


def check_from_oregon(db, site):
    async def go(sessions):
        async with http_client(site) as client:
            return await run_due_checks(sessions, client, OREGON, recheck_after=0)
    return run(go, db)


def downs(db):
    db.expire_all()
    return db.scalars(select(Incident).where(Incident.kind == "down", Incident.ended_at.is_(None))).all()


def test_a_site_another_region_can_load_is_not_called_down(db, project, site):
    project.check_regions = True
    db.add(SiteCheck(project_id=project.id, path="/", ok=True, status=200, ms=300, region="frankfurt",
                     ts=minutes_ago(2)))
    db.commit()
    site.answers = [503]
    check_from_oregon(db, site)
    assert downs(db) == []
    assert db.scalars(select(SiteCheck).where(SiteCheck.region == "oregon")).one().ok is False


def test_when_the_other_regions_agree_it_is_down(db, project, site):
    project.check_regions = True
    db.add(SiteCheck(project_id=project.id, path="/", ok=False, status=503, region="frankfurt", ts=minutes_ago(1)))
    db.add(SiteCheck(project_id=project.id, path="/", ok=False, status=503, region="singapore", ts=minutes_ago(1)))
    db.commit()
    site.answers = [503]
    check_from_oregon(db, site)
    down, = downs(db)
    assert "We tried twice from 3 places." in down.what


def test_without_regions_one_place_decides_as_before(db, project, site):
    db.add(SiteCheck(project_id=project.id, path="/", ok=True, status=200, region="frankfurt", ts=minutes_ago(1)))
    db.commit()
    site.answers = [503]
    check_from_oregon(db, site)
    assert "We tried twice from outside." in downs(db)[0].what


def test_a_probe_records_checks_for_projects_with_regions_on_their_own_schedule(db, project, site):
    async def go(sessions):
        async with http_client(site) as client:
            last = {}
            first = await probe_once(sessions, client, FRANKFURT, last)
            again = await probe_once(sessions, client, FRANKFURT, last)
            return first, again

    assert run(go, db) == (0, 0), "regions are off for this project"
    project.check_regions = True
    db.commit()
    assert run(go, db) == (1, 0), "checked once, then not again until its interval has passed"
    check = db.scalars(select(SiteCheck)).one()
    assert (check.region, check.path, check.ok) == ("frankfurt", "/", True)
    assert check.ts > dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
