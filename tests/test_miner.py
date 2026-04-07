import os
import tempfile
import shutil
import yaml
import chromadb
from mempalace.miner import mine


def test_project_mining():
    tmpdir = tempfile.mkdtemp()
    # Create a mini project
    os.makedirs(os.path.join(tmpdir, "backend"))
    with open(os.path.join(tmpdir, "backend", "app.py"), "w") as f:
        f.write("def main():\n    print('hello world')\n" * 20)
    # Create config
    with open(os.path.join(tmpdir, "mempalace.yaml"), "w") as f:
        yaml.dump(
            {
                "wing": "test_project",
                "rooms": [
                    {"name": "backend", "description": "Backend code"},
                    {"name": "general", "description": "General"},
                ],
            },
            f,
        )

    palace_path = os.path.join(tmpdir, "palace")
    mine(tmpdir, palace_path)

    # Verify
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    assert col.count() > 0

    shutil.rmtree(tmpdir)


def test_project_mining_skips_unchanged_files_without_duplication():
    tmpdir = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmpdir, "backend"))
    file_path = os.path.join(tmpdir, "backend", "app.py")
    with open(file_path, "w") as f:
        f.write("def main():\n    print('hello world')\n" * 20)

    with open(os.path.join(tmpdir, "mempalace.yaml"), "w") as f:
        yaml.dump(
            {
                "wing": "test_project",
                "rooms": [
                    {"name": "backend", "description": "Backend code"},
                    {"name": "general", "description": "General"},
                ],
            },
            f,
        )

    palace_path = os.path.join(tmpdir, "palace")
    mine(tmpdir, palace_path)

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    first_count = col.count()
    first_rows = col.get(where={"source_file": file_path}, include=["metadatas"])
    first_hashes = {m["source_hash"] for m in first_rows["metadatas"]}

    mine(tmpdir, palace_path)

    second_count = col.count()
    second_rows = col.get(where={"source_file": file_path}, include=["metadatas"])
    second_hashes = {m["source_hash"] for m in second_rows["metadatas"]}

    assert second_count == first_count
    assert second_hashes == first_hashes

    shutil.rmtree(tmpdir)


def test_project_mining_reindexes_changed_files():
    tmpdir = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmpdir, "backend"))
    file_path = os.path.join(tmpdir, "backend", "app.py")
    with open(file_path, "w") as f:
        f.write("def main():\n    print('hello world')\n" * 20)

    with open(os.path.join(tmpdir, "mempalace.yaml"), "w") as f:
        yaml.dump(
            {
                "wing": "test_project",
                "rooms": [
                    {"name": "backend", "description": "Backend code"},
                    {"name": "general", "description": "General"},
                ],
            },
            f,
        )

    palace_path = os.path.join(tmpdir, "palace")
    mine(tmpdir, palace_path)

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    first_rows = col.get(where={"source_file": file_path}, include=["documents", "metadatas"])
    first_hashes = {m["source_hash"] for m in first_rows["metadatas"]}

    with open(file_path, "w") as f:
        f.write("def main():\n    print('updated world')\n" * 20)

    mine(tmpdir, palace_path)

    second_rows = col.get(where={"source_file": file_path}, include=["documents", "metadatas"])
    second_hashes = {m["source_hash"] for m in second_rows["metadatas"]}
    second_docs = second_rows["documents"]

    assert second_hashes != first_hashes
    assert any("updated world" in doc for doc in second_docs)

    shutil.rmtree(tmpdir)
