import os
import json
import tempfile
from mempalace.config import MempalaceConfig


def test_default_config():
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert "palace" in cfg.palace_path
    assert cfg.collection_name == "mempalace_drawers"
    assert cfg.notion["enabled"] is False
    assert cfg.notion["wing"] == "wing_notion"


def test_config_from_file():
    tmpdir = tempfile.mkdtemp()
    with open(os.path.join(tmpdir, "config.json"), "w") as f:
        json.dump(
            {
                "palace_path": "/custom/palace",
                "notion": {"enabled": True, "wing": "wing_workspace"},
            },
            f,
        )
    cfg = MempalaceConfig(config_dir=tmpdir)
    assert cfg.palace_path == "/custom/palace"
    assert cfg.notion["enabled"] is True
    assert cfg.notion["wing"] == "wing_workspace"
    assert cfg.notion["server_url"] == "https://mcp.notion.com"


def test_env_override():
    os.environ["MEMPALACE_PALACE_PATH"] = "/env/palace"
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert cfg.palace_path == "/env/palace"
    del os.environ["MEMPALACE_PALACE_PATH"]


def test_init():
    tmpdir = tempfile.mkdtemp()
    cfg = MempalaceConfig(config_dir=tmpdir)
    cfg.init()
    assert os.path.exists(os.path.join(tmpdir, "config.json"))
    with open(os.path.join(tmpdir, "config.json"), "r") as f:
        payload = json.load(f)
    assert "notion" in payload
