import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

from prodbat_server import queue, worker
from prodbat_server.models import Dependency, Incident, Project, QueuedJob

from .helpers import NOW, SETTINGS, days_ago, http_client, minutes_ago, run, run_jobs


def jobs(db):
    db.expire_all()
    return db.scalars(select(QueuedJob).order_by(QueuedJob.id)).all()


def enqueue(db, kind, project_id=None, **kw):
    async def go(sessions):
        async with sessions() as s:
            await queue.enqueue(s, kind, project_id, **kw)
            await s.commit()

    run(go, db)


def run_due(db, site=None):
    async def go(sessions):
        async with http_client(site) if site else _nothing() as client:
            return await queue.run_due(sessions, client, SETTINGS)

    return run(go, db)


class _nothing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def handled(monkeypatch):
    """A job kind that records its runs and fails while `failing` is set."""
    calls = {"runs": [], "failing": None}

    async def handler(sessions, client, settings, job):
        calls["runs"].append(job.key)
        if calls["failing"]:
            raise calls["failing"]

    monkeypatch.setitem(queue.HANDLERS, "test", handler)
    return calls


def test_enqueueing_a_waiting_job_again_does_nothing_but_can_move_it_sooner(db, project):
    later = NOW + dt.timedelta(hours=1)
    enqueue(db, "check_packages", project.id, run_after=later)
    enqueue(db, "check_packages", project.id, run_after=later + dt.timedelta(hours=1))
    job, = jobs(db)
    assert (job.key, job.project_id, job.run_after) == (str(project.id), project.id, later)

    enqueue(db, "check_packages", project.id)
    job, = jobs(db)
    assert job.run_after <= dt.datetime.now(dt.UTC)
    # A different kind or key is a job of its own.
    enqueue(db, "test", key="a")
    enqueue(db, "test", key="b")
    assert len(jobs(db)) == 3


def test_a_job_runs_once_it_is_due_and_is_then_finished(db, handled):
    enqueue(db, "test", key="later", run_after=NOW + dt.timedelta(minutes=5))
    assert run_due(db) == 0 and handled["runs"] == []
    enqueue(db, "test", key="now")
    assert run_due(db) == 1 and handled["runs"] == ["now"]
    done, waiting = sorted(jobs(db), key=lambda j: j.key != "now")
    assert done.finished_at is not None and not done.failed and done.attempts == 1
    assert waiting.finished_at is None and waiting.attempts == 0


def test_two_workers_cannot_claim_the_same_job(db, handled):
    enqueue(db, "test", key="one")

    async def go(sessions):
        async with sessions() as first, sessions() as second:
            # The first worker holds the row; the second skips it rather than wait.
            await first.execute(select(QueuedJob).with_for_update())
            assert await queue.claim(second) is None
            await first.rollback()
            job = await queue.claim(second)
            assert job is not None and job.attempts == 1 and job.locked_at is not None
            # Claimed and running: nobody else gets it.
            assert await queue.claim(first) is None

    run(go, db)


def test_a_failing_job_backs_off_and_is_given_up_after_four_attempts(db, handled):
    handled["failing"] = RuntimeError("deps.dev said no")
    enqueue(db, "test", key="x")
    waits = []
    for attempt in range(1, 5):
        assert run_due(db) == 1
        job, = jobs(db)
        assert job.attempts == attempt and job.last_error == "RuntimeError: deps.dev said no"
        if attempt < 4:
            assert job.finished_at is None and job.locked_at is None
            waits.append(round((job.run_after - dt.datetime.now(dt.UTC)).total_seconds() / 60))
            assert run_due(db) == 0  # not due yet
            job.run_after = NOW - dt.timedelta(seconds=1)
            db.commit()
    assert waits == [1, 10, 60]
    assert job.finished_at is not None and job.failed
    assert run_due(db) == 0 and len(handled["runs"]) == 4


def test_a_job_whose_worker_died_is_taken_up_again(db, handled):
    enqueue(db, "test", key="x")
    job, = jobs(db)
    job.locked_at, job.attempts = minutes_ago(5), 1
    db.commit()
    assert run_due(db) == 0  # its worker may still be on it
    job.locked_at = minutes_ago(11)
    db.commit()
    assert run_due(db) == 1 and handled["runs"] == ["x"]
    job, = jobs(db)
    assert job.finished_at is not None and job.attempts == 2


def test_a_change_while_a_job_runs_gets_a_run_of_its_own(db, handled):
    enqueue(db, "test", key="x")

    async def go(sessions):
        async with sessions() as s:
            await queue.claim(s)
            # The package list is sent again while the first check is running.
            await queue.enqueue(s, "test", key="x")
            await s.commit()

    run(go, db)
    running, waiting = jobs(db)
    assert running.locked_at is not None and waiting.locked_at is None
    # The running one fails: the waiting one does the work instead, and no retry is kept.
    handled["failing"] = ValueError("boom")
    running.locked_at = minutes_ago(11)
    db.commit()
    assert run_due(db) == 2
    first, second = jobs(db)
    assert first.finished_at is not None and not first.failed and first.last_error == "ValueError: boom"
    assert second.attempts == 1 and second.finished_at is None


def test_sending_the_package_list_enqueues_a_check_that_runs_straight_away(client, db, project, key, site):
    client.put("/api/dependencies", headers=key, json={"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.20"}]})
    job, = jobs(db)
    assert (job.kind, job.project_id, job.finished_at) == ("check_packages", project.id, None)
    # Sending it again before the check runs does not add a second one.
    client.put("/api/dependencies", headers=key, json={"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.20"}]})
    assert len(jobs(db)) == 1

    site.osv = {
        "/v1/querybatch": {"results": [{"vulns": [{"id": "GHSA-35jh"}]}]},
        "/v1/vulns/GHSA-35jh": {"id": "GHSA-35jh", "summary": "Command injection.",
                                "database_specific": {"severity": "HIGH"},
                                "affected": [{"package": {"name": "lodash"}, "ranges": [{"events": [{"fixed": "4.17.21"}]}]}]},
    }
    assert run_due(db, site) == 1
    incident = db.scalars(select(Incident)).one()
    assert (incident.kind, incident.key, incident.data["to"]) == ("advisory", "npm/lodash", "4.17.21")
    db.refresh(project)
    assert project.advisories_checked_at is not None and project.freshness_checked_at is not None
    assert jobs(db)[0].finished_at is not None

    # A new language version from the running server is checked too.
    client.put("/api/runtime", headers=key, json={"language": "node", "version": "22.1.0"})
    assert [j.finished_at is None for j in jobs(db)] == [False, True]


def test_the_hourly_pass_enqueues_sites_not_checked_in_a_day(db, project):
    def add(domain, checked, paused=False):
        p = Project(domain=domain, advisories_checked_at=checked, freshness_checked_at=checked,
                    paused_at=NOW if paused else None)
        db.add(p)
        db.commit()
        return p

    fresh = add("fresh.com", minutes_ago(30))
    stale = add("stale.com", days_ago(2))
    add("paused.com", None, paused=True)
    half = add("half.com", minutes_ago(30))
    half.freshness_checked_at = None
    db.commit()
    # `project` has never been checked.
    assert run(queue.enqueue_due_checks, db) == 3
    assert {j.project_id for j in jobs(db)} == {project.id, stale.id, half.id}
    assert fresh.id not in {j.project_id for j in jobs(db)}
    # Again an hour later, before they have run: still one each.
    run(queue.enqueue_due_checks, db)
    assert len(jobs(db)) == 3


def test_finished_jobs_are_pruned_after_a_week(db, project):
    db.add_all([QueuedJob(kind="test", key="old", finished_at=days_ago(8)),
                QueuedJob(kind="test", key="recent", finished_at=days_ago(6)),
                QueuedJob(kind="test", key="waiting", run_after=days_ago(9))])
    db.commit()
    run(lambda sessions: worker.prune(sessions, SETTINGS), db)
    assert [j.key for j in jobs(db)] == ["recent", "waiting"]


def test_the_worker_starts_a_job_as_soon_as_it_is_enqueued(db, handled):
    async def go(sessions):
        loop = asyncio.create_task(worker.queue_loop(sessions, None, SETTINGS))
        try:
            await asyncio.sleep(0.5)  # idle, listening
            async with sessions() as s:
                await queue.enqueue(s, "test", key="now")
                await s.commit()
            for _ in range(40):
                if handled["runs"]:
                    return True
                await asyncio.sleep(0.05)
            return False
        finally:
            loop.cancel()

    assert run(go, db), "the job did not start within two seconds"
