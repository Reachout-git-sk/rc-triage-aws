import psycopg
import pytest

from migrations.migrate import apply
from tests.conftest import APP_PASSWORD, APP_USER, DQ_USER

pytestmark = pytest.mark.db


def test_migrations_are_idempotent(admin_url):
    assert apply(admin_url, app_user=APP_USER, app_password=APP_PASSWORD, dq_user=DQ_USER) == []


def test_app_role_cannot_delete_or_change_schema(app_url):
    with psycopg.connect(app_url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM failures")
    with psycopg.connect(app_url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DROP TABLE failures")
