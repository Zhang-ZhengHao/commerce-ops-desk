from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from postgres_tests.harness import (
    TemporaryPostgresDatabase,
    load_postgres_admin_url,
    temporary_postgres_database,
)


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    try:
        admin_url = load_postgres_admin_url()
    except ValueError as error:
        error_message = str(error)
    else:
        return admin_url

    pytest.fail(error_message, pytrace=False)


@pytest.fixture
def postgres_database(postgres_admin_url: str) -> Iterator[TemporaryPostgresDatabase]:
    with temporary_postgres_database(postgres_admin_url) as database:
        yield database


@pytest.fixture
def postgres_engine(postgres_database: TemporaryPostgresDatabase) -> Engine:
    engine = create_engine(postgres_database.url, pool_pre_ping=True)
    return postgres_database.register_engine(engine)
