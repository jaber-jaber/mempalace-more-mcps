"""
notion_integration.py — Live Notion MCP integration for MemPalace.

This module keeps the HTTP/OAuth/MCP details in one place so the rest of the
codebase can treat Notion as another palace-backed wing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import chromadb
import httpx

from .config import MempalaceConfig
from .miner import chunk_text

MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0
NOTION_SOURCE_TYPE = "notion"
NOTION_COMMENT_SOURCE_TYPE = "notion_comment"
REQUIRED_TOOLS = ("notion-search", "notion-fetch")
DEFAULT_SYNC_SEED_QUERIES = ["a", "e", "i", "o", "u", "the"]
WELL_KNOWN_OAUTH = "/.well-known/oauth-authorization-server"
WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"


class NotionIntegrationError(Exception):
    """Base class for Notion integration failures."""


class NotionAuthRequired(NotionIntegrationError):
    """Raised when Notion has not been connected yet."""


class NotionTransportError(NotionIntegrationError):
    """Raised when the MCP transport fails."""


class NotionToolMissingError(NotionIntegrationError):
    """Raised when the Notion MCP server does not expose required tools."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _read_json(path: str, default: Optional[dict] = None) -> dict:
    try:
        with open(Path(path).expanduser(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def _write_json_secure(path: str, payload: dict) -> None:
    target = Path(path).expanduser()
    _ensure_parent_dir(str(target))
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def _delete_if_exists(path: str) -> bool:
    target = Path(path).expanduser()
    if not target.exists():
        return False
    target.unlink()
    return True


def _is_notion_url(url: Optional[str]) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return "notion." in host or host.endswith("notion.so")


def _normalize_notion_url_or_id(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (notion_url, page_id) from either a full Notion URL or a raw page id."""
    if not value:
        return None, None
    cleaned = value.strip()
    if not cleaned:
        return None, None
    if _is_notion_url(cleaned):
        return cleaned, _page_id_from_value(cleaned)
    page_id = _page_id_from_value(cleaned)
    if page_id:
        return None, page_id
    return cleaned, None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "general"


def _page_id_from_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})", value)
    if not match:
        return None
    page_id = match.group(1).replace("-", "").lower()
    if len(page_id) != 32:
        return None
    return page_id


def _iter_dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            yield cleaned
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)


def _first_non_empty(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_title(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        texts = [_extract_title(item) for item in value]
        texts = [item for item in texts if item]
        if texts:
            return " ".join(texts)
    if isinstance(value, dict):
        return _first_non_empty(
            value.get("title"),
            value.get("plain_text"),
            value.get("name"),
            value.get("text"),
            value.get("content"),
        )
    return None


def _parse_tool_content(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    if "structuredContent" in payload:
        return payload["structuredContent"]

    if "content" not in payload:
        return payload

    content = payload.get("content") or []
    text_parts = []
    json_objects = []

    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            text = item["text"]
            text_parts.append(text)
            try:
                json_objects.append(json.loads(text))
            except json.JSONDecodeError:
                pass
        elif item.get("type") == "json":
            json_objects.append(item.get("json"))

    if len(json_objects) == 1:
        return json_objects[0]
    if len(json_objects) > 1:
        return json_objects
    if text_parts:
        combined = "\n\n".join(text_parts).strip()
        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            return {"text": combined}
    return payload


def _extract_workspace_hint(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        workspace_id = _first_non_empty(
            payload.get("workspace_id"),
            payload.get("workspaceId"),
            payload.get("id") if payload.get("object") == "workspace" else None,
        )
        workspace_name = _first_non_empty(
            payload.get("workspace_name"),
            payload.get("workspaceName"),
            payload.get("name"),
            _extract_title(payload.get("workspace")),
        )
        if workspace_id or workspace_name:
            return {
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
            }

    for item in _iter_dicts(payload):
        workspace_id = _first_non_empty(
            item.get("workspace_id"),
            item.get("workspaceId"),
        )
        workspace_name = _first_non_empty(
            item.get("workspace_name"),
            item.get("workspaceName"),
            item.get("name"),
        )
        if workspace_id or workspace_name:
            return {
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
            }

    return {}


def parse_notion_search_results(payload: Any) -> List[Dict[str, Any]]:
    parsed = _parse_tool_content(payload)
    candidates = []

    for item in _iter_dicts(parsed):
        raw_url = _first_non_empty(item.get("url"), item.get("href"), item.get("link"))
        url, url_page_id = _normalize_notion_url_or_id(raw_url)
        item_id = _first_non_empty(
            item.get("page_id"),
            item.get("notion_page_id"),
            item.get("id"),
        )
        _, item_page_id = _normalize_notion_url_or_id(item_id)
        page_id = item_page_id or url_page_id
        object_type = _first_non_empty(
            item.get("object"),
            item.get("object_type"),
            item.get("type"),
            item.get("kind"),
        )
        title = _first_non_empty(
            item.get("title"),
            item.get("name"),
            _extract_title(item.get("properties")),
        )
        last_edited_time = _first_non_empty(
            item.get("last_edited_time"),
            item.get("lastEditedTime"),
            item.get("updated_at"),
        )
        room_hint = _first_non_empty(
            item.get("database_title"),
            item.get("data_source_title"),
            item.get("parent_title"),
            item.get("teamspace_title"),
            _extract_title(item.get("database")),
            _extract_title(item.get("data_source")),
            _extract_title(item.get("parent")),
        )

        if not page_id and not url:
            continue

        if url and not _is_notion_url(url):
            continue

        normalized_type = (object_type or "").lower()
        if normalized_type and normalized_type not in {
            "page",
            "database",
            "data_source",
            "document",
            "notion_page",
        }:
            continue

        key = page_id or url
        if key in {candidate["key"] for candidate in candidates}:
            continue

        candidates.append(
            {
                "key": key,
                "page_id": page_id,
                "url": url,
                "title": title or "Untitled",
                "object_type": object_type or "page",
                "last_edited_time": last_edited_time,
                "room_hint": room_hint,
            }
        )

    return candidates


def parse_notion_fetch_result(payload: Any) -> Dict[str, Any]:
    parsed = _parse_tool_content(payload)
    workspace = _extract_workspace_hint(parsed)

    metadata = {
        "title": None,
        "url": None,
        "page_id": None,
        "object_type": None,
        "last_edited_time": None,
        "database_title": None,
        "parent_title": None,
        "teamspace_title": None,
        "workspace_id": workspace.get("workspace_id"),
        "workspace_name": workspace.get("workspace_name"),
    }

    for item in _iter_dicts(parsed):
        raw_url = _first_non_empty(item.get("url"), item.get("href"))
        url, url_page_id = _normalize_notion_url_or_id(raw_url)
        item_id = _first_non_empty(
            item.get("page_id"),
            item.get("notion_page_id"),
            item.get("id"),
        )
        _, item_page_id = _normalize_notion_url_or_id(item_id)
        metadata["title"] = metadata["title"] or _first_non_empty(
            item.get("title"),
            item.get("name"),
            _extract_title(item.get("properties")),
        )
        metadata["url"] = metadata["url"] or url
        metadata["page_id"] = metadata["page_id"] or item_page_id or url_page_id
        metadata["object_type"] = metadata["object_type"] or _first_non_empty(
            item.get("object"),
            item.get("object_type"),
            item.get("type"),
        )
        metadata["last_edited_time"] = metadata["last_edited_time"] or _first_non_empty(
            item.get("last_edited_time"),
            item.get("lastEditedTime"),
            item.get("updated_at"),
        )
        metadata["database_title"] = metadata["database_title"] or _first_non_empty(
            item.get("database_title"),
            item.get("data_source_title"),
            _extract_title(item.get("database")),
            _extract_title(item.get("data_source")),
        )
        metadata["parent_title"] = metadata["parent_title"] or _first_non_empty(
            item.get("parent_title"),
            _extract_title(item.get("parent")),
        )
        metadata["teamspace_title"] = metadata["teamspace_title"] or _first_non_empty(
            item.get("teamspace_title"),
            _extract_title(item.get("teamspace")),
        )

    page_text = None
    if isinstance(parsed, dict):
        page_text = _first_non_empty(
            parsed.get("markdown"),
            parsed.get("plain_text"),
            parsed.get("text"),
            parsed.get("body") if isinstance(parsed.get("body"), str) else None,
        )

    if not page_text:
        preferred = []
        if isinstance(parsed, dict):
            for key in ("markdown", "content", "body", "children", "results", "items", "text"):
                if key in parsed:
                    preferred.extend(list(_iter_strings(parsed[key])))
        else:
            preferred.extend(list(_iter_strings(parsed)))
        page_text = "\n".join(dict.fromkeys(preferred)).strip()

    if not page_text:
        page_text = json.dumps(parsed, indent=2, sort_keys=True)

    metadata["title"] = metadata["title"] or "Untitled"
    metadata["object_type"] = metadata["object_type"] or "page"
    metadata["page_id"] = metadata["page_id"] or _page_id_from_value(metadata["url"])

    return {
        "metadata": metadata,
        "text": page_text.strip(),
        "raw": parsed,
    }


def normalize_notion_page_text(page: Dict[str, Any]) -> str:
    metadata = page["metadata"]
    lines = [
        f"# {metadata.get('title') or 'Untitled'}",
        f"Source: Notion {metadata.get('object_type') or 'page'}",
    ]
    if metadata.get("url"):
        lines.append(f"URL: {metadata['url']}")
    if metadata.get("last_edited_time"):
        lines.append(f"Last edited: {metadata['last_edited_time']}")
    if metadata.get("workspace_name"):
        lines.append(f"Workspace: {metadata['workspace_name']}")
    lines.append("")
    lines.append(page["text"])
    return "\n".join(lines).strip()


def normalize_notion_comments_text(page: Dict[str, Any], comments_payload: Any) -> str:
    if comments_payload in (None, "", {}, []):
        return ""

    if isinstance(comments_payload, str):
        body = comments_payload.strip()
    elif isinstance(comments_payload, dict) and isinstance(comments_payload.get("text"), str):
        body = comments_payload["text"].strip()
    else:
        body = json.dumps(comments_payload, indent=2, sort_keys=True)

    if not body:
        return ""

    metadata = page["metadata"]
    lines = [
        f"# {metadata.get('title') or 'Untitled'}",
        "Source: Notion comments",
    ]
    if metadata.get("url"):
        lines.append(f"URL: {metadata['url']}")
    if metadata.get("last_edited_time"):
        lines.append(f"Page last edited: {metadata['last_edited_time']}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines).strip()


def pick_notion_room(page: Dict[str, Any]) -> str:
    metadata = page["metadata"]
    room_name = (
        metadata.get("database_title")
        or metadata.get("parent_title")
        or metadata.get("teamspace_title")
        or "general"
    )
    return _slugify(room_name)


def _sse_events(lines: Iterable[str]) -> Iterable[tuple[Optional[str], str]]:
    event = None
    data_lines: List[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
                event = None
                data_lines = []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event, "\n".join(data_lines)


class StreamableHttpMcpTransport:
    def __init__(self, endpoint_url: str, access_token: str, timeout: float = DEFAULT_TIMEOUT):
        self.endpoint_url = endpoint_url
        self.access_token = access_token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        self._request_id = 0

    def connect(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mempalace", "version": "3.0.0"},
            },
        )
        if isinstance(result, dict):
            self.protocol_version = result.get("protocolVersion", MCP_PROTOCOL_VERSION)
        self.notify("notifications/initialized", {})

    def close(self) -> None:
        try:
            headers = self._headers()
            self.client.delete(self.endpoint_url, headers=headers)
        except Exception:
            pass
        self.client.close()

    def request(self, method: str, params: dict) -> Any:
        self._request_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        headers = self._headers()
        response = self.client.post(self.endpoint_url, headers=headers, json=message)
        if response.status_code >= 400:
            raise NotionTransportError(f"HTTP {response.status_code}: {response.text}")
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        if not response.content:
            return None
        return self._parse_response(response, self._request_id)

    def notify(self, method: str, params: dict) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        headers = self._headers()
        response = self.client.post(self.endpoint_url, headers=headers, json=message)
        if response.status_code >= 400:
            raise NotionTransportError(f"HTTP {response.status_code}: {response.text}")

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _parse_response(self, response: httpx.Response, expected_id: int) -> Any:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = response.json()
            if "error" in payload:
                raise NotionTransportError(payload["error"].get("message", "MCP request failed"))
            return payload.get("result")

        if "text/event-stream" in content_type:
            for event_name, data in _sse_events(response.iter_lines()):
                if event_name == "endpoint":
                    continue
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") != expected_id:
                    continue
                if "error" in payload:
                    raise NotionTransportError(payload["error"].get("message", "MCP request failed"))
                return payload.get("result")

        raise NotionTransportError("Unsupported MCP response format")


class LegacySseMcpTransport:
    def __init__(self, sse_url: str, access_token: str, timeout: float = DEFAULT_TIMEOUT):
        self.sse_url = sse_url
        self.access_token = access_token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        self._request_id = 0
        self._message_endpoint: Optional[str] = None
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._stream_context = None
        self._stream_response = None
        self._reader_thread: Optional[threading.Thread] = None

    def connect(self) -> None:
        self._stream_context = self.client.stream(
            "GET",
            self.sse_url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": self.protocol_version,
            },
        )
        self._stream_response = self._stream_context.__enter__()
        if self._stream_response.status_code >= 400:
            raise NotionTransportError(
                f"HTTP {self._stream_response.status_code}: {self._stream_response.text}"
            )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        endpoint_event = self._wait_for_queue("endpoint", timeout=self.timeout)
        self._message_endpoint = urljoin(self.sse_url, endpoint_event["data"])
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mempalace", "version": "3.0.0"},
            },
        )
        if isinstance(result, dict):
            self.protocol_version = result.get("protocolVersion", MCP_PROTOCOL_VERSION)
        self.notify("notifications/initialized", {})

    def close(self) -> None:
        self._stop_event.set()
        try:
            if self._stream_context is not None:
                self._stream_context.__exit__(None, None, None)
        except Exception:
            pass
        self.client.close()

    def request(self, method: str, params: dict) -> Any:
        if not self._message_endpoint:
            raise NotionTransportError("SSE message endpoint is not ready")
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response = self.client.post(self._message_endpoint, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise NotionTransportError(f"HTTP {response.status_code}: {response.text}")
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("id") == request_id:
                if "error" in body:
                    raise NotionTransportError(body["error"].get("message", "MCP request failed"))
                return body.get("result")
        item = self._wait_for_queue("message", timeout=self.timeout, request_id=request_id)
        message = item["data"]
        if "error" in message:
            raise NotionTransportError(message["error"].get("message", "MCP request failed"))
        return message.get("result")

    def notify(self, method: str, params: dict) -> None:
        if not self._message_endpoint:
            raise NotionTransportError("SSE message endpoint is not ready")
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        response = self.client.post(self._message_endpoint, headers=self._headers(), json=payload)
        if response.status_code >= 400:
            raise NotionTransportError(f"HTTP {response.status_code}: {response.text}")

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _read_loop(self) -> None:
        try:
            for event_name, data in _sse_events(self._stream_response.iter_lines()):
                if self._stop_event.is_set():
                    break
                if event_name == "endpoint":
                    self._queue.put({"kind": "endpoint", "data": data})
                    continue
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                self._queue.put({"kind": "message", "data": payload})
        except Exception as exc:
            self._queue.put({"kind": "error", "data": str(exc)})

    def _wait_for_queue(
        self,
        kind: str,
        timeout: float,
        request_id: Optional[int] = None,
    ) -> dict:
        deadline = time.time() + timeout
        buffered = []
        while time.time() < deadline:
            try:
                item = self._queue.get(timeout=min(0.25, max(deadline - time.time(), 0.01)))
            except queue.Empty:
                continue
            if item["kind"] == "error":
                raise NotionTransportError(item["data"])
            if item["kind"] != kind:
                buffered.append(item)
                continue
            if request_id is not None and item["data"].get("id") != request_id:
                buffered.append(item)
                continue
            for buffered_item in buffered:
                self._queue.put(buffered_item)
            return item
        raise NotionTransportError("Timed out waiting for SSE MCP response")


class NotionMcpClient:
    def __init__(self, config: Optional[MempalaceConfig] = None):
        self.config = config or MempalaceConfig()
        self.notion_config = self.config.notion
        self.auth_path = self.notion_config["auth_file"]
        self.auth_state = _read_json(self.auth_path, default={})
        self.transport: Optional[Any] = None
        self.tool_schemas: Dict[str, dict] = {}

    def __enter__(self) -> "NotionMcpClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None

    def connect(self) -> None:
        if not self.auth_state or "tokens" not in self.auth_state:
            raise NotionAuthRequired("Run `mempalace notion connect` first.")

        self._refresh_access_token_if_needed()
        access_token = self.auth_state["tokens"]["access_token"]
        base_server = self.notion_config["server_url"].rstrip("/")
        if base_server.endswith("/mcp"):
            server_root = base_server[: -len("/mcp")]
        elif base_server.endswith("/sse"):
            server_root = base_server[: -len("/sse")]
        else:
            server_root = base_server

        streamable_endpoint = base_server if base_server.endswith("/mcp") else f"{base_server}/mcp"
        sse_endpoint = base_server if base_server.endswith("/sse") else f"{server_root}/sse"

        transport_errors = []
        for transport_name, factory in (
            ("streamable_http", lambda: StreamableHttpMcpTransport(streamable_endpoint, access_token)),
            ("legacy_sse", lambda: LegacySseMcpTransport(sse_endpoint, access_token)),
        ):
            transport = None
            try:
                transport = factory()
                transport.connect()
                self.transport = transport
                self.auth_state.setdefault("transport", {})["last_transport"] = transport_name
                self.auth_state["transport"]["connected_at"] = utc_now_iso()
                self._persist_auth_state()
                return
            except Exception as exc:
                transport_errors.append(f"{transport_name}: {exc}")
                try:
                    if transport is not None:
                        transport.close()
                except Exception:
                    pass

        raise NotionTransportError(" | ".join(transport_errors))

    def list_tools(self) -> List[dict]:
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        self.tool_schemas = {tool["name"]: tool for tool in tools if isinstance(tool, dict)}
        return tools

    def ensure_required_tools(self) -> Dict[str, dict]:
        if not self.tool_schemas:
            self.list_tools()
        missing = [name for name in REQUIRED_TOOLS if name not in self.tool_schemas]
        if missing:
            raise NotionToolMissingError(f"Missing required Notion MCP tools: {', '.join(missing)}")
        self.auth_state["tools"] = self.tool_schemas
        self._persist_auth_state()
        return self.tool_schemas

    def call_tool(self, name: str, arguments: dict) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return _parse_tool_content(result)

    def maybe_get_self(self) -> Dict[str, Any]:
        tool_name = "notion-get-self"
        if tool_name not in self.tool_schemas:
            self.list_tools()
        if tool_name not in self.tool_schemas:
            return {}
        payload = self.call_tool(tool_name, {})
        workspace = _extract_workspace_hint(payload)
        if not workspace:
            workspace = {"workspace_name": _extract_title(payload)}
        return workspace

    def _request(self, method: str, params: dict) -> Any:
        if not self.transport:
            raise NotionTransportError("Notion MCP transport is not connected")
        if method == "tools/call":
            return self.transport.request(method, params)
        if method == "tools/list":
            return self.transport.request(method, params)
        return self.transport.request(method, params)

    def _refresh_access_token_if_needed(self) -> None:
        tokens = self.auth_state.get("tokens") or {}
        expires_at = _parse_datetime(tokens.get("expires_at"))
        if expires_at and expires_at - utc_now() > timedelta(minutes=5):
            return

        metadata = self.auth_state.get("metadata") or {}
        token_endpoint = metadata.get("token_endpoint")
        refresh_token = tokens.get("refresh_token")
        client_id = self.auth_state.get("client", {}).get("client_id")
        client_secret = self.auth_state.get("client", {}).get("client_secret")
        if not token_endpoint or not refresh_token or not client_id:
            raise NotionAuthRequired("Notion credentials are incomplete. Reconnect Notion.")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret

        response = httpx.post(token_endpoint, data=data, timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        if response.status_code >= 400:
            raise NotionAuthRequired(f"Unable to refresh Notion token: {response.text}")
        payload = response.json()
        tokens["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            tokens["refresh_token"] = payload["refresh_token"]
        expires_in = payload.get("expires_in")
        if expires_in:
            tokens["expires_at"] = (utc_now() + timedelta(seconds=int(expires_in))).isoformat()
        self.auth_state["tokens"] = tokens
        self._persist_auth_state()

    def _persist_auth_state(self) -> None:
        self.auth_state["updated_at"] = utc_now_iso()
        _write_json_secure(self.auth_path, self.auth_state)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    callback_queue: "queue.Queue[dict]" = queue.Queue()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        payload = {key: values[0] for key, values in params.items()}
        self.callback_queue.put(payload)
        html = (
            "<html><body><h1>MemPalace Notion connection complete.</h1>"
            "<p>You can return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class NotionWingService:
    def __init__(self, config: Optional[MempalaceConfig] = None):
        self.config = config or MempalaceConfig()
        self.notion_config = self.config.notion

    def connect(self, timeout: int = 300) -> Dict[str, Any]:
        metadata = self.discover_oauth_metadata(self.notion_config["server_url"])
        self._drain_callback_queue()

        callback_server = HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
        callback_port = callback_server.server_port
        redirect_uri = f"http://127.0.0.1:{callback_port}/callback"
        callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
        callback_thread.start()

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(32)

        client_registration = self.register_client(metadata, redirect_uri)
        client_id = client_registration["client_id"]
        client_secret = client_registration.get("client_secret")

        auth_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid offline_access",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.notion_config["server_url"],
        }
        auth_url = f"{metadata['authorization_endpoint']}?{urlencode(auth_params)}"
        webbrowser.open(auth_url)
        print(f"\nOpen this URL if your browser did not launch:\n{auth_url}\n")

        callback_payload = self._wait_for_callback(timeout)
        callback_server.shutdown()
        callback_server.server_close()

        if callback_payload.get("state") != state:
            raise NotionIntegrationError("Notion OAuth state mismatch")
        if callback_payload.get("error"):
            raise NotionIntegrationError(callback_payload["error"])
        code = callback_payload.get("code")
        if not code:
            raise NotionIntegrationError("Notion did not return an authorization code")

        token_payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        if client_secret:
            token_payload["client_secret"] = client_secret
        token_response = httpx.post(
            metadata["token_endpoint"],
            data=token_payload,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if token_response.status_code >= 400:
            raise NotionIntegrationError(
                f"Notion token exchange failed: {token_response.status_code} {token_response.text}"
            )
        tokens = token_response.json()
        expires_at = None
        if tokens.get("expires_in"):
            expires_at = (utc_now() + timedelta(seconds=int(tokens["expires_in"]))).isoformat()

        auth_state = {
            "metadata": metadata,
            "client": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            "tokens": {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": expires_at,
            },
            "connected_at": utc_now_iso(),
        }

        _write_json_secure(self.notion_config["auth_file"], auth_state)

        workspace = {}
        tool_schemas = {}
        with NotionMcpClient(self.config) as client:
            tool_schemas = client.ensure_required_tools()
            workspace = client.maybe_get_self()
            if workspace:
                client.auth_state["workspace"] = workspace
                client._persist_auth_state()

        cache_state = self._read_cache_state()
        cache_state["workspace"] = workspace
        cache_state["tools"] = sorted(tool_schemas.keys())
        cache_state["connected_at"] = utc_now_iso()
        self._write_cache_state(cache_state)

        return {
            "success": True,
            "workspace": workspace,
            "tools": sorted(tool_schemas.keys()),
            "server_url": self.notion_config["server_url"],
        }

    def disconnect(self) -> Dict[str, Any]:
        auth_removed = _delete_if_exists(self.notion_config["auth_file"])
        cache_removed = _delete_if_exists(self.notion_config["cache_state_file"])
        return {
            "success": True,
            "auth_removed": auth_removed,
            "cache_removed": cache_removed,
        }

    def status(self, palace_path: Optional[str] = None) -> Dict[str, Any]:
        palace_path = palace_path or self.config.palace_path
        auth_state = _read_json(self.notion_config["auth_file"], default={})
        cache_state = self._read_cache_state()
        cached = self.cached_counts(palace_path)

        status = {
            "enabled": bool(self.notion_config.get("enabled")),
            "connected": bool(auth_state.get("tokens")),
            "wing": self.notion_config["wing"],
            "server_url": self.notion_config["server_url"],
            "live_refresh": bool(self.notion_config.get("live_refresh", True)),
            "cached_pages": cached["pages"],
            "cached_drawers": cached["drawers"],
            "cached_comment_drawers": cached["comment_drawers"],
            "workspace": auth_state.get("workspace") or cache_state.get("workspace") or {},
            "last_refresh_at": cache_state.get("last_refresh_at"),
            "last_refresh_query": cache_state.get("last_refresh_query"),
            "last_sync_at": cache_state.get("last_sync_at"),
            "last_sync_query": cache_state.get("last_sync_query"),
            "last_sync_limit": cache_state.get("last_sync_limit"),
            "last_transport": (auth_state.get("transport") or {}).get("last_transport"),
        }

        if auth_state.get("tokens", {}).get("expires_at"):
            status["expires_at"] = auth_state["tokens"]["expires_at"]

        return status

    def cached_counts(self, palace_path: str) -> Dict[str, int]:
        try:
            client = chromadb.PersistentClient(path=palace_path)
            collection = client.get_collection(self.config.collection_name)
            page_results = collection.get(
                where={"source_type": NOTION_SOURCE_TYPE},
                include=["metadatas"],
            )
            comment_results = collection.get(
                where={"source_type": NOTION_COMMENT_SOURCE_TYPE},
                include=["metadatas"],
            )
        except Exception:
            return {"drawers": 0, "pages": 0, "comment_drawers": 0}

        metas = page_results.get("metadatas", [])
        page_ids = {meta.get("notion_page_id") for meta in metas if meta.get("notion_page_id")}
        return {
            "drawers": len(metas),
            "pages": len(page_ids),
            "comment_drawers": len(comment_results.get("metadatas", [])),
        }

    def hydrate_search_results(
        self,
        query: str,
        palace_path: str,
        wing: Optional[str],
        limit: int,
        refresh_notion: bool = True,
    ) -> Dict[str, Any]:
        if not self.should_query_notion(wing=wing, refresh_notion=refresh_notion):
            return {"hydrated": 0, "warnings": []}

        with NotionMcpClient(self.config) as client:
            tools = client.ensure_required_tools()
            search_tool = tools["notion-search"]
            fetch_tool = tools["notion-fetch"]
            comments_tool = tools.get("notion-get-comments")

            search_args = self._build_search_arguments(search_tool.get("inputSchema", {}), query, limit)
            search_payload = client.call_tool("notion-search", search_args)
            targets = parse_notion_search_results(search_payload)

            hydrated = 0
            fetched_pages = []
            for target in targets[:limit]:
                if not self._should_refresh_target(palace_path, target):
                    continue
                fetch_args = self._build_fetch_arguments(fetch_tool.get("inputSchema", {}), target)
                fetch_payload = client.call_tool("notion-fetch", fetch_args)
                page = parse_notion_fetch_result(fetch_payload)
                if not page["metadata"].get("page_id"):
                    page["metadata"]["page_id"] = target.get("page_id")
                if not page["metadata"].get("url"):
                    page["metadata"]["url"] = target.get("url")
                if not page["metadata"].get("last_edited_time"):
                    page["metadata"]["last_edited_time"] = target.get("last_edited_time")
                if not page["metadata"].get("title"):
                    page["metadata"]["title"] = target.get("title")
                self.store_notion_page(page, palace_path)
                self._sync_comments_for_page(
                    client=client,
                    comments_tool=comments_tool,
                    page=page,
                    palace_path=palace_path,
                )
                hydrated += 1
                fetched_pages.append(page["metadata"].get("page_id"))

            cache_state = self._read_cache_state()
            cache_state["last_refresh_at"] = utc_now_iso()
            cache_state["last_refresh_query"] = query
            cache_state["last_refreshed_page_ids"] = [page_id for page_id in fetched_pages if page_id]
            self._write_cache_state(cache_state)

            return {"hydrated": hydrated, "warnings": []}

    def sync(
        self,
        palace_path: str,
        query: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        Bulk-hydrate Notion pages returned by a broad workspace search.

        This is intentionally simple: it fetches the pages surfaced by Notion's
        own search tool, then stores anything new or stale in the local wing.
        """
        if not self.notion_config.get("enabled"):
            raise NotionIntegrationError("Enable Notion in ~/.mempalace/config.json first.")

        if limit <= 0:
            raise NotionIntegrationError("Sync limit must be greater than 0.")

        queries = [query.strip()] if query.strip() else DEFAULT_SYNC_SEED_QUERIES

        with NotionMcpClient(self.config) as client:
            tools = client.ensure_required_tools()
            search_tool = tools["notion-search"]
            fetch_tool = tools["notion-fetch"]
            comments_tool = tools.get("notion-get-comments")

            targets = []
            seen_target_ids = set()
            per_query_limit = max(1, min(25, limit))

            for seed_query in queries:
                if len(targets) >= limit:
                    break
                remaining = max(1, min(25, limit - len(targets)))
                search_args = self._build_search_arguments(
                    search_tool.get("inputSchema", {}),
                    seed_query,
                    min(per_query_limit, remaining),
                )
                search_payload = client.call_tool("notion-search", search_args)
                for target in parse_notion_search_results(search_payload):
                    dedupe_key = target.get("page_id") or target.get("url") or target.get("title")
                    if dedupe_key in seen_target_ids:
                        continue
                    seen_target_ids.add(dedupe_key)
                    targets.append(target)
                    if len(targets) >= limit:
                        break

            considered = 0
            synced = 0
            skipped = 0
            synced_page_ids = []

            for target in targets[:limit]:
                considered += 1
                if not self._should_refresh_target(palace_path, target):
                    page = self._page_from_existing_or_target(palace_path, target)
                    if page is not None:
                        self._sync_comments_for_page(
                            client=client,
                            comments_tool=comments_tool,
                            page=page,
                            palace_path=palace_path,
                        )
                    skipped += 1
                    continue

                fetch_args = self._build_fetch_arguments(fetch_tool.get("inputSchema", {}), target)
                fetch_payload = client.call_tool("notion-fetch", fetch_args)
                page = parse_notion_fetch_result(fetch_payload)
                if not page["metadata"].get("page_id"):
                    page["metadata"]["page_id"] = target.get("page_id")
                if not page["metadata"].get("url"):
                    page["metadata"]["url"] = target.get("url")
                if not page["metadata"].get("last_edited_time"):
                    page["metadata"]["last_edited_time"] = target.get("last_edited_time")
                if not page["metadata"].get("title"):
                    page["metadata"]["title"] = target.get("title")
                self.store_notion_page(page, palace_path)
                self._sync_comments_for_page(
                    client=client,
                    comments_tool=comments_tool,
                    page=page,
                    palace_path=palace_path,
                )
                synced += 1
                if page["metadata"].get("page_id"):
                    synced_page_ids.append(page["metadata"]["page_id"])

            cache_state = self._read_cache_state()
            cache_state["last_sync_at"] = utc_now_iso()
            cache_state["last_sync_query"] = query
            cache_state["last_sync_queries_used"] = queries
            cache_state["last_sync_limit"] = limit
            cache_state["last_sync_considered"] = considered
            cache_state["last_synced_page_ids"] = synced_page_ids
            self._write_cache_state(cache_state)

            return {
                "success": True,
                "query": query,
                "queries_used": queries,
                "limit": limit,
                "considered": considered,
                "synced": synced,
                "skipped": skipped,
            }

    def store_notion_page(self, page: Dict[str, Any], palace_path: str) -> int:
        notion_wing = self.notion_config["wing"]
        room = pick_notion_room(page)
        text = normalize_notion_page_text(page)
        page_id = page["metadata"].get("page_id")
        if not page_id:
            raise NotionIntegrationError("Notion page is missing a stable page identifier")

        client = chromadb.PersistentClient(path=palace_path)
        collection = client.get_or_create_collection(self.config.collection_name)

        chunks = chunk_text(text, page["metadata"].get("url") or page_id)
        if not chunks:
            return 0

        ids = []
        documents = []
        metadatas = []

        for chunk in chunks:
            ids.append(f"notion_{page_id}_{chunk['chunk_index']}")
            documents.append(chunk["content"])
            metadatas.append(
                {
                    "wing": notion_wing,
                    "room": room,
                    "source_file": page["metadata"].get("url") or page_id,
                    "source_type": NOTION_SOURCE_TYPE,
                    "source_uri": page["metadata"].get("url") or page_id,
                    "chunk_index": chunk["chunk_index"],
                    "added_by": "notion_mcp",
                    "filed_at": utc_now_iso(),
                    "notion_page_id": page_id,
                    "notion_url": page["metadata"].get("url") or "",
                    "notion_last_edited_time": page["metadata"].get("last_edited_time") or "",
                    "notion_workspace_id": page["metadata"].get("workspace_id") or "",
                    "notion_title": page["metadata"].get("title") or "Untitled",
                    "notion_object_type": page["metadata"].get("object_type") or "page",
                }
            )

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(ids)

    def store_notion_comments(self, page: Dict[str, Any], comments_payload: Any, palace_path: str) -> int:
        page_id = page["metadata"].get("page_id")
        if not page_id:
            return 0

        comments_text = normalize_notion_comments_text(page, comments_payload)
        client = chromadb.PersistentClient(path=palace_path)
        collection = client.get_or_create_collection(self.config.collection_name)
        self._delete_comment_drawers(collection, page_id)

        if not comments_text:
            return 0

        room = pick_notion_room(page)
        chunks = chunk_text(comments_text, f"notion_comments:{page_id}")
        if not chunks:
            return 0

        comment_hash = hashlib.sha256(comments_text.encode("utf-8")).hexdigest()
        ids = []
        documents = []
        metadatas = []
        for chunk in chunks:
            ids.append(f"notion_comment_{page_id}_{chunk['chunk_index']}")
            documents.append(chunk["content"])
            metadatas.append(
                {
                    "wing": self.notion_config["wing"],
                    "room": room,
                    "source_file": page["metadata"].get("url") or page_id,
                    "source_type": NOTION_COMMENT_SOURCE_TYPE,
                    "source_uri": page["metadata"].get("url") or page_id,
                    "chunk_index": chunk["chunk_index"],
                    "added_by": "notion_mcp",
                    "filed_at": utc_now_iso(),
                    "notion_page_id": page_id,
                    "notion_url": page["metadata"].get("url") or "",
                    "notion_last_edited_time": page["metadata"].get("last_edited_time") or "",
                    "notion_workspace_id": page["metadata"].get("workspace_id") or "",
                    "notion_title": page["metadata"].get("title") or "Untitled",
                    "notion_object_type": "comment_thread",
                    "notion_comment_hash": comment_hash,
                }
            )

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(ids)

    def should_query_notion(self, wing: Optional[str], refresh_notion: bool) -> bool:
        if not self.notion_config.get("enabled"):
            return False
        if not refresh_notion:
            return False
        notion_wing = self.notion_config["wing"]
        return wing in (None, notion_wing)

    def _should_refresh_target(self, palace_path: str, target: Dict[str, Any]) -> bool:
        if not self.notion_config.get("live_refresh", True):
            return False

        page_id = target.get("page_id")
        if not page_id:
            return True

        try:
            client = chromadb.PersistentClient(path=palace_path)
            collection = client.get_collection(self.config.collection_name)
            results = collection.get(
                where={"$and": [{"source_type": NOTION_SOURCE_TYPE}, {"notion_page_id": page_id}]},
                include=["metadatas"],
            )
        except Exception:
            return True

        metas = results.get("metadatas", [])
        if not metas:
            return True

        local_last_edited = metas[0].get("notion_last_edited_time")
        remote_last_edited = target.get("last_edited_time")
        if not remote_last_edited:
            return False
        return local_last_edited != remote_last_edited

    def _build_search_arguments(self, schema: dict, query: str, limit: int) -> dict:
        properties = schema.get("properties", {}) or {}
        args = {}
        query_key = self._first_matching_key(properties, ["query", "search", "q", "text"])
        if query_key:
            args[query_key] = query
        limit_key = self._first_matching_key(
            properties,
            ["page_size", "limit", "n", "max_results", "num_results"],
        )
        if limit_key:
            args[limit_key] = limit
        query_type_key = self._first_matching_key(properties, ["query_type"])
        if query_type_key and query_type_key not in args:
            args[query_type_key] = "internal"
        filters_key = self._first_matching_key(properties, ["filters"])
        if filters_key and filters_key not in args:
            args[filters_key] = {}
        return args

    def _build_fetch_arguments(self, schema: dict, target: Dict[str, Any]) -> dict:
        properties = schema.get("properties", {}) or {}
        args = {}
        url_value = target.get("url")
        id_value = target.get("page_id")

        for key in properties:
            lowered = key.lower()
            if "url" in lowered and url_value:
                args[key] = url_value
                return args
            if lowered in {"page_id", "id", "page", "target", "resource"} and id_value:
                args[key] = id_value
                return args
        if url_value:
            args["url"] = url_value
        elif id_value:
            args["id"] = id_value
        return args

    def _first_matching_key(self, properties: dict, candidates: List[str]) -> Optional[str]:
        if not properties:
            return None
        lowered = {key.lower(): key for key in properties}
        for candidate in candidates:
            if candidate in lowered:
                return lowered[candidate]
        return next(iter(properties.keys()), None)

    def _sync_comments_for_page(
        self,
        client: NotionMcpClient,
        comments_tool: Optional[dict],
        page: Dict[str, Any],
        palace_path: str,
    ) -> None:
        if not comments_tool:
            return
        page_id = page["metadata"].get("page_id")
        if not page_id:
            return
        comments_payload = client.call_tool(
            "notion-get-comments",
            {
                "page_id": page_id,
                "include_all_blocks": True,
                "include_resolved": True,
            },
        )
        self.store_notion_comments(page, comments_payload, palace_path)

    def _delete_comment_drawers(self, collection, page_id: str) -> int:
        try:
            results = collection.get(
                where={
                    "$and": [
                        {"source_type": NOTION_COMMENT_SOURCE_TYPE},
                        {"notion_page_id": page_id},
                    ]
                }
            )
            ids = results.get("ids", [])
            if ids:
                collection.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0

    def _page_from_existing_or_target(self, palace_path: str, target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        page_id = target.get("page_id")
        if not page_id:
            return None
        try:
            client = chromadb.PersistentClient(path=palace_path)
            collection = client.get_collection(self.config.collection_name)
            results = collection.get(
                where={
                    "$and": [
                        {"source_type": NOTION_SOURCE_TYPE},
                        {"notion_page_id": page_id},
                    ]
                },
                include=["metadatas"],
            )
        except Exception:
            results = {"metadatas": []}

        metas = results.get("metadatas", [])
        meta = metas[0] if metas else {}
        return {
            "metadata": {
                "page_id": page_id,
                "title": meta.get("notion_title") or target.get("title") or "Untitled",
                "url": meta.get("notion_url") or target.get("url"),
                "object_type": meta.get("notion_object_type") or target.get("object_type") or "page",
                "last_edited_time": meta.get("notion_last_edited_time") or target.get("last_edited_time"),
                "database_title": target.get("room_hint"),
                "workspace_id": meta.get("notion_workspace_id"),
            },
            "text": "",
        }

    def _wait_for_callback(self, timeout: int) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return _OAuthCallbackHandler.callback_queue.get(timeout=0.25)
            except queue.Empty:
                continue
        raise NotionIntegrationError("Timed out waiting for the Notion OAuth callback")

    def _drain_callback_queue(self) -> None:
        while True:
            try:
                _OAuthCallbackHandler.callback_queue.get_nowait()
            except queue.Empty:
                return

    def discover_oauth_metadata(self, server_url: str) -> Dict[str, Any]:
        server_url = server_url.rstrip("/")
        protected_resource = httpx.get(
            f"{server_url}{WELL_KNOWN_PROTECTED_RESOURCE}",
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if protected_resource.status_code >= 400:
            raise NotionIntegrationError(
                f"Unable to discover Notion protected resource metadata: {protected_resource.text}"
            )
        protected_resource_metadata = protected_resource.json()

        issuer = protected_resource_metadata.get("authorization_servers", [server_url])[0].rstrip("/")
        oauth_metadata = httpx.get(
            f"{issuer}{WELL_KNOWN_OAUTH}",
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if oauth_metadata.status_code >= 400:
            raise NotionIntegrationError(
                f"Unable to discover Notion OAuth metadata: {oauth_metadata.text}"
            )
        payload = oauth_metadata.json()
        payload["issuer"] = payload.get("issuer", issuer)
        payload["registration_endpoint"] = payload.get(
            "registration_endpoint", f"{payload['issuer'].rstrip('/')}/register"
        )
        payload["resource"] = server_url
        return payload

    def register_client(self, metadata: Dict[str, Any], redirect_uri: str) -> Dict[str, Any]:
        response = httpx.post(
            metadata["registration_endpoint"],
            json={
                "client_name": "MemPalace",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            raise NotionIntegrationError(
                f"Unable to register the MemPalace Notion client: {response.text}"
            )
        return response.json()

    def _read_cache_state(self) -> dict:
        return _read_json(self.notion_config["cache_state_file"], default={})

    def _write_cache_state(self, payload: dict) -> None:
        _write_json_secure(self.notion_config["cache_state_file"], payload)
