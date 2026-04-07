import json
import os
import tempfile

import chromadb
import httpx

from mempalace.config import MempalaceConfig
from mempalace.notion_integration import (
    LegacySseMcpTransport,
    NotionIntegrationError,
    NotionMcpClient,
    NotionWingService,
    NOTION_COMMENT_SOURCE_TYPE,
    StreamableHttpMcpTransport,
    parse_notion_fetch_result,
    parse_notion_search_results,
    pick_notion_room,
)
from mempalace.searcher import search_memories


def make_config(tmpdir, notion_enabled=True, live_refresh=True):
    config = {
        "palace_path": os.path.join(tmpdir, "palace"),
        "collection_name": "mempalace_drawers",
        "notion": {
            "enabled": notion_enabled,
            "wing": "wing_notion",
            "server_url": "https://mcp.notion.com",
            "live_refresh": live_refresh,
            "auth_file": os.path.join(tmpdir, "notion_auth.json"),
            "cache_state_file": os.path.join(tmpdir, "notion_cache_state.json"),
        },
    }
    with open(os.path.join(tmpdir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)
    return MempalaceConfig(config_dir=tmpdir)


def seed_local_doc(palace_path, text, wing="wing_local", room="general"):
    client = chromadb.PersistentClient(path=palace_path)
    collection = client.get_or_create_collection("mempalace_drawers")
    collection.add(
        ids=[f"{wing}_{room}_1"],
        documents=[text],
        metadatas=[
            {
                "wing": wing,
                "room": room,
                "source_file": "local.txt",
                "chunk_index": 0,
                "added_by": "test",
                "filed_at": "2026-04-07T00:00:00+00:00",
            }
        ],
    )


def test_parse_notion_search_results_filters_non_notion_items():
    payload = {
        "results": [
            {
                "object": "page",
                "id": "01234567-89ab-cdef-0123-456789abcdef",
                "url": "https://www.notion.so/Test-0123456789abcdef0123456789abcdef",
                "title": "Project Notes",
                "last_edited_time": "2026-04-07T10:00:00.000Z",
            },
            {
                "object": "external_result",
                "url": "https://slack.com/test",
                "title": "Ignore me",
            },
        ]
    }

    results = parse_notion_search_results(payload)
    assert len(results) == 1
    assert results[0]["page_id"] == "0123456789abcdef0123456789abcdef"

def test_parse_notion_fetch_result_and_room_mapping():
    page = parse_notion_fetch_result(
        {
            "title": "Sprint Plan",
            "object": "page",
            "url": "https://www.notion.so/Sprint-Plan-0123456789abcdef0123456789abcdef",
            "last_edited_time": "2026-04-07T12:00:00.000Z",
            "database_title": "Roadmap",
            "markdown": "Ship the Notion integration.",
        }
    )

    assert page["metadata"]["page_id"] == "0123456789abcdef0123456789abcdef"
    assert page["text"] == "Ship the Notion integration."
    assert pick_notion_room(page) == "roadmap"


def test_store_notion_page_upserts_existing_page():
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    service = NotionWingService(cfg)

    first_page = {
        "metadata": {
            "page_id": "0123456789abcdef0123456789abcdef",
            "title": "Design Notes",
            "url": "https://www.notion.so/page-0123456789abcdef0123456789abcdef",
            "object_type": "page",
            "last_edited_time": "2026-04-07T10:00:00.000Z",
            "database_title": "Architecture",
            "workspace_id": "ws_123",
        },
        "text": "Initial version of the page.",
    }
    second_page = {
        "metadata": {
            "page_id": "0123456789abcdef0123456789abcdef",
            "title": "Design Notes",
            "url": "https://www.notion.so/page-0123456789abcdef0123456789abcdef",
            "object_type": "page",
            "last_edited_time": "2026-04-08T10:00:00.000Z",
            "database_title": "Architecture",
            "workspace_id": "ws_123",
        },
        "text": "Updated version of the page with more detail.",
    }

    first_count = service.store_notion_page(first_page, cfg.palace_path)
    second_count = service.store_notion_page(second_page, cfg.palace_path)

    client = chromadb.PersistentClient(path=cfg.palace_path)
    collection = client.get_collection(cfg.collection_name)
    results = collection.get(
        where={"$and": [{"source_type": "notion"}, {"notion_page_id": "0123456789abcdef0123456789abcdef"}]},
        include=["documents", "metadatas"],
    )

    assert first_count == second_count
    assert len(results["ids"]) == second_count
    assert any("Updated version" in doc for doc in results["documents"])
    assert all(meta["room"] == "architecture" for meta in results["metadatas"])


def test_store_notion_comments_creates_comment_drawers():
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    service = NotionWingService(cfg)

    page = {
        "metadata": {
            "page_id": "0123456789abcdef0123456789abcdef",
            "title": "Design Notes",
            "url": "https://www.notion.so/page-0123456789abcdef0123456789abcdef",
            "object_type": "page",
            "last_edited_time": "2026-04-08T10:00:00.000Z",
            "database_title": "Architecture",
            "workspace_id": "ws_123",
        },
        "text": "Body",
    }

    count = service.store_notion_comments(
        page,
        {"text": "<discussion><comment>Looks good</comment></discussion>"},
        cfg.palace_path,
    )

    client = chromadb.PersistentClient(path=cfg.palace_path)
    collection = client.get_collection(cfg.collection_name)
    results = collection.get(
        where={"source_type": NOTION_COMMENT_SOURCE_TYPE},
        include=["documents", "metadatas"],
    )

    assert count == len(results["ids"])
    assert any("Looks good" in doc for doc in results["documents"])
    assert all(meta["source_type"] == NOTION_COMMENT_SOURCE_TYPE for meta in results["metadatas"])


def test_search_memories_skips_notion_for_non_notion_wing(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    seed_local_doc(cfg.palace_path, "local memory about auth decisions", wing="wing_code")

    class FakeNotionService:
        def __init__(self, config):
            self.config = config

        def should_query_notion(self, wing, refresh_notion):
            return wing in (None, "wing_notion")

        def hydrate_search_results(self, **kwargs):
            raise AssertionError("Notion should not be queried for a non-Notion wing")

    monkeypatch.setattr("mempalace.searcher.MempalaceConfig", lambda: cfg)
    monkeypatch.setattr("mempalace.searcher.NotionWingService", FakeNotionService)

    result = search_memories(
        "auth decisions",
        palace_path=cfg.palace_path,
        wing="wing_code",
        n_results=3,
        refresh_notion=True,
    )

    assert not result["warnings"]
    assert result["results"]
    assert all(hit["wing"] == "wing_code" for hit in result["results"])


def test_search_memories_hydrates_notion_results(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    seed_local_doc(cfg.palace_path, "local code context", wing="wing_code")
    real_service = NotionWingService(cfg)

    class FakeNotionService:
        def __init__(self, config):
            self.config = config

        def should_query_notion(self, wing, refresh_notion):
            return True

        def hydrate_search_results(self, **kwargs):
            page = {
                "metadata": {
                    "page_id": "fedcba9876543210fedcba9876543210",
                    "title": "Auth Decision",
                    "url": "https://www.notion.so/auth-fedcba9876543210fedcba9876543210",
                    "object_type": "page",
                    "last_edited_time": "2026-04-07T11:00:00.000Z",
                    "database_title": "Decisions",
                },
                "text": "We switched to Clerk because of the maintenance burden.",
            }
            real_service.store_notion_page(page, cfg.palace_path)
            return {"hydrated": 1, "warnings": []}

    monkeypatch.setattr("mempalace.searcher.MempalaceConfig", lambda: cfg)
    monkeypatch.setattr("mempalace.searcher.NotionWingService", FakeNotionService)

    result = search_memories(
        "Clerk maintenance burden",
        palace_path=cfg.palace_path,
        n_results=5,
        refresh_notion=True,
    )

    assert result["hydrated_notion_pages"] == 1
    assert any(hit["source_type"] == "notion" for hit in result["results"])


def test_search_memories_warns_when_notion_refresh_fails(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    seed_local_doc(cfg.palace_path, "memory about deployment", wing="wing_code")

    class FakeNotionService:
        def __init__(self, config):
            self.config = config

        def should_query_notion(self, wing, refresh_notion):
            return True

        def hydrate_search_results(self, **kwargs):
            raise NotionIntegrationError("rate limit")

    monkeypatch.setattr("mempalace.searcher.MempalaceConfig", lambda: cfg)
    monkeypatch.setattr("mempalace.searcher.NotionWingService", FakeNotionService)

    result = search_memories(
        "deployment",
        palace_path=cfg.palace_path,
        n_results=3,
        refresh_notion=True,
    )

    assert result["results"]
    assert result["warnings"]
    assert "Notion refresh skipped" in result["warnings"][0]

def test_discover_oauth_metadata(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    service = NotionWingService(cfg)

    def fake_get(url, **kwargs):
        if url.endswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200,
                json={"authorization_servers": ["https://auth.example.com"]},
            )
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.com",
                    "authorization_endpoint": "https://auth.example.com/authorize",
                    "token_endpoint": "https://auth.example.com/token",
                    "registration_endpoint": "https://auth.example.com/register",
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("mempalace.notion_integration.httpx.get", fake_get)
    metadata = service.discover_oauth_metadata("https://mcp.notion.com")

    assert metadata["authorization_endpoint"] == "https://auth.example.com/authorize"
    assert metadata["token_endpoint"] == "https://auth.example.com/token"
    assert metadata["resource"] == "https://mcp.notion.com"


def test_register_client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    service = NotionWingService(cfg)

    def fake_post(url, **kwargs):
        assert url == "https://auth.example.com/register"
        payload = kwargs["json"]
        assert payload["redirect_uris"] == ["http://127.0.0.1:9999/callback"]
        return httpx.Response(200, json={"client_id": "client_123", "client_secret": "secret"})

    monkeypatch.setattr("mempalace.notion_integration.httpx.post", fake_post)
    registration = service.register_client(
        {
            "registration_endpoint": "https://auth.example.com/register",
        },
        "http://127.0.0.1:9999/callback",
    )

    assert registration["client_id"] == "client_123"


def test_refresh_access_token(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    auth_state = {
        "metadata": {"token_endpoint": "https://auth.example.com/token"},
        "client": {"client_id": "client_123", "client_secret": "secret"},
        "tokens": {
            "access_token": "old_access",
            "refresh_token": "refresh_123",
            "expires_at": "2020-01-01T00:00:00+00:00",
        },
    }
    with open(cfg.notion["auth_file"], "w", encoding="utf-8") as f:
        json.dump(auth_state, f)

    def fake_post(url, **kwargs):
        assert kwargs["data"]["grant_type"] == "refresh_token"
        return httpx.Response(
            200,
            json={
                "access_token": "new_access",
                "refresh_token": "refresh_456",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr("mempalace.notion_integration.httpx.post", fake_post)
    client = NotionMcpClient(cfg)
    client._refresh_access_token_if_needed()

    refreshed = json.load(open(cfg.notion["auth_file"], "r", encoding="utf-8"))
    assert refreshed["tokens"]["access_token"] == "new_access"
    assert refreshed["tokens"]["refresh_token"] == "refresh_456"


def test_transport_fallback_to_legacy_sse(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    cfg = make_config(tmpdir)
    auth_state = {
        "metadata": {"token_endpoint": "https://auth.example.com/token"},
        "client": {"client_id": "client_123"},
        "tokens": {"access_token": "access", "refresh_token": "refresh"},
    }
    with open(cfg.notion["auth_file"], "w", encoding="utf-8") as f:
        json.dump(auth_state, f)

    class FakeStreamable(StreamableHttpMcpTransport):
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise NotionIntegrationError("streamable unavailable")

        def close(self):
            return

    class FakeLegacy(LegacySseMcpTransport):
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return

        def close(self):
            return

    monkeypatch.setattr("mempalace.notion_integration.StreamableHttpMcpTransport", FakeStreamable)
    monkeypatch.setattr("mempalace.notion_integration.LegacySseMcpTransport", FakeLegacy)
    monkeypatch.setattr(
        NotionMcpClient,
        "_refresh_access_token_if_needed",
        lambda self: None,
    )

    client = NotionMcpClient(cfg)
    client.connect()
    assert isinstance(client.transport, FakeLegacy)
