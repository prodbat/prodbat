import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prodbat_server.app import create_app
from prodbat_server.models import Base, Project, hash_secret

from .helpers import BROWSER, SETTINGS, TEST_DB, Site, http_client


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DB)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        yield session


@pytest.fixture
def project(db):
    project = Project(domain="yourapp.com", key="pb_test", alert_email="you@example.com",
                      secret_hash=hash_secret("pbs_test"))
    db.add(project)
    db.commit()
    return project


@pytest.fixture(autouse=True)
def forget_what_was_looked_up():
    """Advisories are remembered between passes; not between tests."""
    from prodbat_server import advisories

    advisories._details.clear()
    advisories._scores.clear()


@pytest.fixture
def site():
    """The founder's site, as seen from outside. Tests change how it answers."""
    return Site()


@pytest.fixture
def client(db, site):
    app = create_app(SETTINGS, http=http_client(site))
    with TestClient(app, headers={"user-agent": BROWSER}) as client:
        yield client


@pytest.fixture
def key():
    return {"authorization": "Bearer pbs_test"}
