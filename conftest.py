"""Test-suite-wide guard: no test reaches a real database unless it says so.

`api.py` calls load_dotenv() on import, so on a developer's machine the .env's
SUPABASE_DB_URL is in the environment for every test that imports it. A test that saves
anything then writes to the REAL database. That happened: one test posted to /portfolios under a
fixed owner key on every run, leaving a junk row each time until the per-key cap was hit and the
test began failing - a failure that looked like a code bug and was pollution of production data.

So, for every test except those marked `regression` (which exist to hit real services, and
guard themselves with their own skip conditions), the database URLs are removed from the
environment. A test that wants storage configured sets a URL itself, and the ones that do
point it at a dead address on purpose.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_database(request, monkeypatch):
    if request.node.get_closest_marker("regression"):
        return
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
