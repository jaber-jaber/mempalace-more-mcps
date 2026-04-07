#!/usr/bin/env python3
"""
miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.
"""

import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import chromadb

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".sh",
    ".csv",
    ".sql",
    ".toml",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
}

CHUNK_SIZE = 800  # chars per drawer
CHUNK_OVERLAP = 100  # overlap between chunks
MIN_CHUNK_SIZE = 50  # skip tiny chunks


# =============================================================================
# CONFIG
# =============================================================================


def load_config(project_dir: str) -> dict:
    """Load mempalace.yaml from project directory (falls back to mempal.yaml)."""
    import yaml

    config_path = Path(project_dir).expanduser().resolve() / "mempalace.yaml"
    if not config_path.exists():
        # Fallback to legacy name
        legacy_path = Path(project_dir).expanduser().resolve() / "mempal.yaml"
        if legacy_path.exists():
            config_path = legacy_path
        else:
            print(f"ERROR: No mempalace.yaml found in {project_dir}")
            print(f"Run: mempalace init {project_dir}")
            sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# =============================================================================
# FILE ROUTING — which room does this file belong to?
# =============================================================================


def detect_room(filepath: Path, content: str, rooms: list, project_path: Path) -> str:
    """
    Route a file to the right room.
    Priority:
    1. Folder path matches a room name
    2. Filename matches a room name or keyword
    3. Content keyword scoring
    4. Fallback: "general"
    """
    relative = str(filepath.relative_to(project_path)).lower()
    filename = filepath.stem.lower()
    content_lower = content[:2000].lower()

    # Priority 1: folder path contains room name
    path_parts = relative.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # skip filename itself
        for room in rooms:
            if room["name"].lower() in part or part in room["name"].lower():
                return room["name"]

    # Priority 2: filename matches room name
    for room in rooms:
        if room["name"].lower() in filename or filename in room["name"].lower():
            return room["name"]

    # Priority 3: keyword scoring from room keywords + name
    scores = defaultdict(int)
    for room in rooms:
        keywords = room.get("keywords", []) + [room["name"]]
        for kw in keywords:
            count = content_lower.count(kw.lower())
            scores[room["name"]] += count

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

    return "general"


# =============================================================================
# CHUNKING
# =============================================================================


def chunk_text(content: str, source_file: str) -> list:
    """
    Split content into drawer-sized chunks.
    Tries to split on paragraph/line boundaries.
    Returns list of {"content": str, "chunk_index": int}
    """
    # Clean up
    content = content.strip()
    if not content:
        return []

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))

        # Try to break at paragraph boundary
        if end < len(content):
            newline_pos = content.rfind("\n\n", start, end)
            if newline_pos > start + CHUNK_SIZE // 2:
                end = newline_pos
            else:
                newline_pos = content.rfind("\n", start, end)
                if newline_pos > start + CHUNK_SIZE // 2:
                    end = newline_pos

        chunk = content[start:end].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(
                {
                    "content": chunk,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1

        start = end - CHUNK_OVERLAP if end < len(content) else end

    return chunks


# =============================================================================
# PALACE — ChromaDB operations
# =============================================================================


def get_collection(palace_path: str):
    os.makedirs(palace_path, exist_ok=True)
    client = chromadb.PersistentClient(path=palace_path)
    try:
        return client.get_collection("mempalace_drawers")
    except Exception:
        return client.create_collection("mempalace_drawers")


def get_file_state(filepath: Path, content: str) -> dict:
    """Return stable metadata used to detect whether a file changed."""
    stat = filepath.stat()
    return {
        "source_mtime": str(stat.st_mtime_ns),
        "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def get_existing_file_metadata(collection, source_file: str) -> list:
    """Fetch existing drawer metadata for one source file."""
    try:
        results = collection.get(where={"source_file": source_file}, include=["metadatas"])
        return results.get("metadatas", [])
    except Exception:
        return []


def file_needs_reindex(collection, source_file: str, file_state: dict) -> tuple[bool, bool]:
    """
    Return (should_index, had_existing_drawers).

    Reindex when:
      - the file has never been mined
      - legacy drawers have no fingerprint metadata
      - the content hash or mtime changed
    """
    existing = get_existing_file_metadata(collection, source_file)
    if not existing:
        return True, False

    sample = existing[0] or {}
    existing_hash = sample.get("source_hash")
    existing_mtime = sample.get("source_mtime")

    if not existing_hash or not existing_mtime:
        return True, True

    is_changed = (
        existing_hash != file_state["source_hash"]
        or existing_mtime != file_state["source_mtime"]
    )
    return is_changed, True


def delete_drawers_for_file(collection, source_file: str) -> int:
    """Delete all drawers associated with one source file."""
    try:
        results = collection.get(where={"source_file": source_file})
        ids = results.get("ids", [])
        if not ids:
            return 0
        collection.delete(ids=ids)
        return len(ids)
    except Exception:
        return 0


def add_drawer(
    collection,
    wing: str,
    room: str,
    content: str,
    source_file: str,
    chunk_index: int,
    agent: str,
    file_state: dict,
):
    """Add one drawer to the palace."""
    drawer_id = f"drawer_{wing}_{room}_{hashlib.md5((source_file + str(chunk_index)).encode()).hexdigest()[:16]}"
    try:
        collection.add(
            documents=[content],
            ids=[drawer_id],
            metadatas=[
                {
                    "wing": wing,
                    "room": room,
                    "source_file": source_file,
                    "chunk_index": chunk_index,
                    "added_by": agent,
                    "filed_at": datetime.now().isoformat(),
                    "source_hash": file_state["source_hash"],
                    "source_mtime": file_state["source_mtime"],
                }
            ],
        )
        return True
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            return False
        raise


# =============================================================================
# PROCESS ONE FILE
# =============================================================================


def process_file(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
) -> dict:
    """Read, chunk, route, and file one file."""

    source_file = str(filepath)

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"status": "unreadable", "drawers": 0, "room": None}

    content = content.strip()
    file_state = get_file_state(filepath, content)

    if not dry_run:
        should_index, had_existing = file_needs_reindex(collection, source_file, file_state)
        if not should_index:
            return {"status": "unchanged", "drawers": 0, "room": None}
    else:
        had_existing = False

    if len(content) < MIN_CHUNK_SIZE:
        if not dry_run and had_existing:
            delete_drawers_for_file(collection, source_file)
            return {"status": "deleted", "drawers": 0, "room": "general"}
        return {"status": "too_small", "drawers": 0, "room": None}

    room = detect_room(filepath, content, rooms, project_path)
    chunks = chunk_text(content, source_file)

    if dry_run:
        print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
        return {"status": "dry_run", "drawers": len(chunks), "room": room}

    if had_existing:
        delete_drawers_for_file(collection, source_file)

    drawers_added = 0
    for chunk in chunks:
        added = add_drawer(
            collection=collection,
            wing=wing,
            room=room,
            content=chunk["content"],
            source_file=source_file,
            chunk_index=chunk["chunk_index"],
            agent=agent,
            file_state=file_state,
        )
        if added:
            drawers_added += 1

    status = "updated" if had_existing else "new"
    return {"status": status, "drawers": drawers_added, "room": room}


# =============================================================================
# SCAN PROJECT
# =============================================================================


def scan_project(project_dir: str) -> list:
    """Return list of all readable file paths."""
    project_path = Path(project_dir).expanduser().resolve()
    files = []
    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            filepath = Path(root) / filename
            if filepath.suffix.lower() in READABLE_EXTENSIONS:
                # Skip config files
                if filename in (
                    "mempalace.yaml",
                    "mempalace.yml",
                    "mempal.yaml",
                    "mempal.yml",
                    ".gitignore",
                    "package-lock.json",
                ):
                    continue
                files.append(filepath)
    return files


# =============================================================================
# MAIN: MINE
# =============================================================================


def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
):
    """Mine a project directory into the palace."""

    project_path = Path(project_dir).expanduser().resolve()
    config = load_config(project_dir)

    wing = wing_override or config["wing"]
    rooms = config.get("rooms", [{"name": "general", "description": "All project files"}])

    files = scan_project(project_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Rooms:   {', '.join(r['name'] for r in rooms)}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'─' * 55}\n")

    if not dry_run:
        collection = get_collection(palace_path)
    else:
        collection = None

    total_drawers = 0
    files_unchanged = 0
    files_updated = 0
    files_new = 0
    files_deleted = 0
    files_unreadable = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        result = process_file(
            filepath=filepath,
            project_path=project_path,
            collection=collection,
            wing=wing,
            rooms=rooms,
            agent=agent,
            dry_run=dry_run,
        )
        status = result["status"]
        drawers = result["drawers"]
        room = result["room"]

        if status == "unchanged":
            files_unchanged += 1
            continue
        if status == "unreadable":
            files_unreadable += 1
            continue
        if status == "deleted":
            files_deleted += 1
            continue

        total_drawers += drawers
        if room:
            room_counts[room] += 1

        if status == "new":
            files_new += 1
        elif status == "updated":
            files_updated += 1

        if not dry_run:
            label = "new" if status == "new" else ("updated" if status == "updated" else status)
            print(f"  ✓ [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers} ({label})")

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {files_new + files_updated}")
    print(f"  New files: {files_new}")
    print(f"  Updated files: {files_updated}")
    print(f"  Unchanged files: {files_unchanged}")
    if files_deleted:
        print(f"  Deleted from palace (now tiny/empty): {files_deleted}")
    if files_unreadable:
        print(f"  Unreadable files: {files_unreadable}")
    print(f"  Drawers filed: {total_drawers}")
    print("\n  By room:")
    for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


# =============================================================================
# STATUS
# =============================================================================


def status(palace_path: str):
    """Show what's been filed in the palace."""
    from .config import MempalaceConfig
    from .notion_integration import NotionWingService

    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        return

    # Count by wing and room
    r = col.get(limit=10000, include=["metadatas"])
    metas = r["metadatas"]

    wing_rooms = defaultdict(lambda: defaultdict(int))
    for m in metas:
        wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1

    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {len(metas)} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()

    notion_status = NotionWingService(MempalaceConfig()).status(palace_path=palace_path)
    if notion_status["enabled"]:
        print("  NOTION")
        print(f"    Connected:      {notion_status['connected']}")
        print(f"    Wing:           {notion_status['wing']}")
        print(f"    Cached pages:   {notion_status['cached_pages']}")
        print(f"    Cached drawers: {notion_status['cached_drawers']}")
        print(f"    Comment drawers:{notion_status['cached_comment_drawers']}")
        if notion_status.get("last_refresh_at"):
            print(f"    Last refresh:   {notion_status['last_refresh_at']}")
        print()
    print(f"{'=' * 55}\n")
