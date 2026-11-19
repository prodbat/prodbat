"""What the public registries say, kept once for every site that uses the
same package version or language release line.

A site's package check reads these first and asks the registries only
about versions whose facts are missing or older than their time below,
in one batch. Facts past their time are still used while they are being
asked about again, and if asking fails.
"""

import datetime as dt

import httpx
from sqlalchemy import select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .models import EolFacts, PackageFacts

# New advisories are published against old versions all the time.
ADVISORIES_TTL = dt.timedelta(hours=24)
# Packages release often, and "outdated" should be about today's release.
LATEST_TTL = dt.timedelta(hours=24)
# Authors rarely deprecate a version they have already released.
DEPRECATED_TTL = dt.timedelta(days=7)
# Release line end dates are announced months ahead.
EOL_TTL = dt.timedelta(days=7)
# A Node.js security release can come out any week.
NODE_RELEASES_TTL = dt.timedelta(hours=24)


def key(thing) -> tuple[str, str, str]:
    return (thing.ecosystem, thing.name, thing.version)


def stale(checked_at: dt.datetime | None, ttl: dt.timedelta, now: dt.datetime) -> bool:
    return checked_at is None or checked_at < now - ttl


async def package_facts(db, deps) -> dict[tuple[str, str, str], PackageFacts]:
    """The facts row for each dependency's exact version, made if new.
    The new rows are committed at once, so another site's check that
    makes the same ones is not kept waiting while this one asks the
    registries."""
    keys = sorted({key(d) for d in deps})
    found: dict[tuple[str, str, str], PackageFacts] = {}
    for start in range(0, len(keys), 1000):
        chunk = keys[start:start + 1000]
        await db.execute(pg_insert(PackageFacts).on_conflict_do_nothing(),
                         [{"ecosystem": e, "name": n, "version": v} for e, n, v in chunk])
        await db.commit()
        rows = await db.scalars(select(PackageFacts).where(
            tuple_(PackageFacts.ecosystem, PackageFacts.name, PackageFacts.version).in_(chunk)))
        found |= {key(row): row for row in rows}
    return {key(d): found[key(d)] for d in deps}  # in the order given


async def eol_facts(db, client: httpx.AsyncClient, product: str, url: str, ttl: dt.timedelta,
                    now: dt.datetime) -> list | None:
    """A product's release lines: kept ones if fresh, else asked for again.
    If asking fails, the kept ones are better than nothing."""
    row = await db.scalar(select(EolFacts).where(EolFacts.product == product))
    if row is not None and not stale(row.checked_at, ttl, now):
        return row.data
    try:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return row.data if row is not None else None
    await db.execute(pg_insert(EolFacts).values(product=product, data=data, checked_at=now).on_conflict_do_update(
        index_elements=["product"], set_={"data": data, "checked_at": now}))
    return data


async def prune(db) -> None:
    """Facts about versions no site has used for a week."""
    await db.execute(text("""
        delete from package_facts f
        where coalesce(f.advisories_checked_at, '-infinity') < now() - interval '7 days'
          and not exists (select 1 from dependencies d
                          where d.ecosystem = f.ecosystem and d.name = f.name and d.version = f.version)"""))
