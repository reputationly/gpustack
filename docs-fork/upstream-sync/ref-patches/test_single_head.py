from alembic.config import Config
from alembic.script import ScriptDirectory
import gpustack, os

def _script():
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(gpustack.__file__), "migrations")
    )
    return ScriptDirectory.from_config(cfg)

def test_single_alembic_head():
    assert _script().get_heads() == ["d9f0a1b2c3e4"]

def test_fork_and_upstream_branches_are_both_ancestors():
    revs = {r.revision for r in _script().iterate_revisions("d9f0a1b2c3e4", "base")}
    assert {"b8d4f6a2c1e9", "a7e2c9d4f681", "e1f2a3b4c5d6", "367a3982fcde"} <= revs

def test_fork_chain_root_is_untouched():
    # 现网库依赖这个父版本；任何构建步骤都不许改写它
    assert _script().get_revision("e1f2a3b4c5d6").down_revision == "c4d7e8f9a0b1"
