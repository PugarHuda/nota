"""docs/skills/SKILL-SPEC.md describes the skills the code actually has, argument for argument."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("skill_spec_md", ROOT / "scripts" / "skill_spec_md.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_published_spec_block_is_exactly_what_the_code_generates():
    mod = _script()
    doc = mod.DOC.read_text(encoding="utf-8")
    block = doc[doc.index(mod.START): doc.index(mod.END) + len(mod.END)]
    assert block == mod.render(), "run: uv run python scripts/skill_spec_md.py"


def test_every_skill_and_every_argument_is_documented():
    from nota.skills import SKILLS

    mod = _script()
    doc = mod.DOC.read_text(encoding="utf-8")
    assert doc.count("### `") >= 2 * len(SKILLS) and set(mod.AVAILABILITY) == set(SKILLS)
    for name, (d, _) in SKILLS.items():
        assert f"### `{name}`" in doc
        assert all(f"| `{a.name}` |" in doc for a in d.args), name
    assert doc.splitlines()[2].startswith("Seven research tools")
