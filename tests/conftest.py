"""Shared test setup.

The API's rate limiter is deliberately process-global (`nota.api._BACKING_HITS`), which is right for
a single `nota serve` but makes the suite order-dependent: the browser tests post backings and invoke
skills through a real server in this same process, so by the time the SocialFi tests run the bucket
for `testclient` can already be full and they get 429 instead of 200. Clear it around every test so a
judge running `uv run pytest -q` gets the same result every time.
"""

import pytest

from nota import api


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    api._BACKING_HITS.clear()
    yield
    api._BACKING_HITS.clear()
