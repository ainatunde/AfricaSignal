"""Alembic history must stay one straight chain (no database needed).

Branches are built in parallel and each adds a migration that points at whatever head its branch
had. Merging them leaves several heads, and ``alembic upgrade head`` then refuses to run.
"""

from alembic.config import Config
from alembic.script import ScriptDirectory


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_exactly_one_alembic_head() -> None:
    heads = _script().get_heads()
    assert len(heads) == 1, (
        f"more than one Alembic head: {sorted(heads)}; re-chain the newest migration"
    )


def test_history_is_one_unbranched_chain_from_the_base() -> None:
    script = _script()
    revisions = list(script.walk_revisions())  # newest first; raises on a broken graph
    assert revisions, "no migrations found"
    assert [r.down_revision for r in revisions if r.down_revision is None] == [None]
    assert all(not r.is_branch_point for r in revisions), "a revision has two children"
    assert all(len(script.get_revisions(r.revision)) == 1 for r in revisions)
    assert len({r.revision for r in revisions}) == len(revisions), "duplicate revision ids"
