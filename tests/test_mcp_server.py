from mempalace import mcp_server


def test_mempalace_search_schema_exposes_refresh_notion():
    schema = mcp_server.TOOLS["mempalace_search"]["input_schema"]
    assert "refresh_notion" in schema["properties"]
    assert schema["properties"]["refresh_notion"]["type"] == "boolean"


def test_tool_search_passes_refresh_notion(monkeypatch):
    captured = {}

    def fake_search_memories(query, palace_path, wing, room, n_results, refresh_notion):
        captured["refresh_notion"] = refresh_notion
        return {"query": query, "results": []}

    monkeypatch.setattr(mcp_server, "search_memories", fake_search_memories)
    mcp_server.tool_search(
        query="auth",
        limit=3,
        wing="wing_notion",
        room="general",
        refresh_notion=False,
    )

    assert captured["refresh_notion"] is False


def test_cli_notion_sync_invokes_service(monkeypatch, capsys):
    from argparse import Namespace
    from mempalace import cli
    import mempalace.notion_integration as notion_integration

    class FakeService:
        def __init__(self, config):
            self.config = config

        def sync(self, palace_path, query, limit):
            assert query == ""
            assert limit == 25
            return {
                "query": query,
                "considered": 5,
                "synced": 3,
                "skipped": 2,
            }

    monkeypatch.setattr(notion_integration, "NotionWingService", FakeService)
    args = Namespace(
        notion_command="sync",
        palace=None,
        query="",
        limit=25,
    )

    cli.cmd_notion(args)
    out = capsys.readouterr().out
    assert "Notion sync complete." in out
    assert "Synced:     3" in out
