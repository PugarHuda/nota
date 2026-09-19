"""Every dotted path Nota reads from RYO must resolve in real RYO answers.

The paths in nota/paths.py were once guessed; a guess that is wrong returns None forever and nothing
fails. Gathering from envelopes RYO actually sent (fixtures/recorded, and the copy in tests/fixtures)
turns such a path into a failing test instead of a silent gap.
"""

from pathlib import Path

import pytest

from nota import paths
from nota.evidence import first_present, gather
from nota.ryo_client import RecordedRyoClient

ROOT = Path(__file__).parent.parent
LISTS = {name: v for name, v in vars(paths).items() if name.isupper() and isinstance(v, list)}


@pytest.mark.parametrize("root,symbol", [(ROOT / "fixtures" / "recorded", s) for s in ("SOL", "BTC", "ETH")]
                         + [(ROOT / "tests" / "fixtures", "SOL")])
def test_every_path_list_resolves_in_real_envelopes(root, symbol):
    pack = gather(RecordedRyoClient(root), symbol)
    assert "FEAR_GREED" in LISTS and "PRICE_USD" in LISTS
    missing = [name for name, candidates in LISTS.items() if first_present(pack, candidates)[1] is None]
    assert missing == [], f"{root.name}/{symbol}: no candidate resolves for {missing}"
    assert isinstance(pack.get(paths.RYO_PLAN), dict)
