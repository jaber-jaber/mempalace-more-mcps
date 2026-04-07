#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Semantic search against the palace.
Returns verbatim text — the actual words, never summaries.
"""

import sys
from pathlib import Path

import chromadb

from .config import MempalaceConfig
from .notion_integration import NotionAuthRequired, NotionIntegrationError, NotionWingService


def _build_where_filter(wing: str = None, room: str = None) -> dict:
    where = {}
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    elif room:
        where = {"room": room}
    return where


def _query_collection(col, query: str, wing: str = None, room: str = None, n_results: int = 5):
    where = _build_where_filter(wing=wing, room=room)
    kwargs = {
        "query_texts": [query],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where
    return col.query(**kwargs)


def search(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    refresh_notion: bool = True,
):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect).
    """
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        sys.exit(1)

    try:
        payload = search_memories(
            query=query,
            palace_path=palace_path,
            wing=wing,
            room=room,
            n_results=n_results,
            refresh_notion=refresh_notion,
        )
    except Exception as e:
        print(f"\n  Search error: {e}")
        sys.exit(1)

    if payload.get("error"):
        print(f"\n  Search error: {payload['error']}")
        sys.exit(1)

    hits = payload.get("results", [])

    if not hits:
        print(f'\n  No results found for: "{query}"')
        for warning in payload.get("warnings", []):
            print(f"  Warning: {warning}")
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    if payload.get("hydrated_notion_pages"):
        print(f"  Notion pages refreshed: {payload['hydrated_notion_pages']}")
    print(f"{'=' * 60}\n")

    for warning in payload.get("warnings", []):
        print(f"  Warning: {warning}")
    if payload.get("warnings"):
        print()

    for i, hit in enumerate(hits, 1):
        similarity = hit.get("similarity", 0)
        source_value = hit.get("source_file") or hit.get("source_uri") or "?"
        source = Path(source_value).name if source_value.startswith("/") else source_value
        wing_name = hit.get("wing", "?")
        room_name = hit.get("room", "?")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  {similarity}")
        if hit.get("source_type") == "notion" and hit.get("notion_url"):
            print(f"      Notion: {hit['notion_url']}")
        print()
        # Print the verbatim text, indented
        for line in hit.get("text", "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'─' * 56}")

    print()


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    refresh_notion: bool = True,
) -> dict:
    """
    Programmatic search — returns a dict instead of printing.
    Used by the MCP server and other callers that need data.
    """
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception as e:
        return {"error": f"No palace found at {palace_path}: {e}"}

    try:
        initial_results = _query_collection(col, query, wing=wing, room=room, n_results=n_results)
    except Exception as e:
        return {"error": f"Search error: {e}"}

    warnings = []
    hydrated_notion_pages = 0
    cfg = MempalaceConfig()
    notion_service = NotionWingService(cfg)
    if notion_service.should_query_notion(wing=wing, refresh_notion=refresh_notion):
        try:
            hydration = notion_service.hydrate_search_results(
                query=query,
                palace_path=palace_path,
                wing=wing,
                limit=n_results,
                refresh_notion=refresh_notion,
            )
            hydrated_notion_pages = hydration.get("hydrated", 0)
            warnings.extend(hydration.get("warnings", []))
            if hydrated_notion_pages:
                initial_results = _query_collection(col, query, wing=wing, room=room, n_results=n_results)
        except NotionAuthRequired as e:
            warnings.append(str(e))
        except NotionIntegrationError as e:
            warnings.append(f"Notion refresh skipped: {e}")
        except Exception as e:
            warnings.append(f"Notion refresh skipped: {e}")

    docs = initial_results["documents"][0]
    metas = initial_results["metadatas"][0]
    dists = initial_results["distances"][0]

    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        hits.append(
            {
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(meta.get("source_file", "?")).name,
                "source_uri": meta.get("source_uri", ""),
                "source_type": meta.get("source_type", "local"),
                "similarity": round(1 - dist, 3),
                "notion_url": meta.get("notion_url", ""),
            }
        )

    return {
        "query": query,
        "filters": {"wing": wing, "room": room},
        "results": hits,
        "warnings": warnings,
        "hydrated_notion_pages": hydrated_notion_pages,
    }
