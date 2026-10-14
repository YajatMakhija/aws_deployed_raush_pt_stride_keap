import pytest

from rpt_agent.services import appointment_views


@pytest.fixture(autouse=True)
def _fresh_table_check():
    # appointment_views remembers which optional tables exist; each test's fake database
    # answers that question its own way.
    appointment_views._TABLES.clear()
    yield
    appointment_views._TABLES.clear()
