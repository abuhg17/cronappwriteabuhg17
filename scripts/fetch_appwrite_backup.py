import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class AppwritePausedError(RuntimeError):
    """Raised when Appwrite pauses the project due to inactivity."""


class AppwriteApiError(RuntimeError):
    def __init__(self, code: int, path: str, body: str, error_type: str | None = None) -> None:
        self.code = code
        self.path = path
        self.body = body
        self.error_type = error_type
        super().__init__(f"Appwrite API error {code} on {path}: {body}")


class AppwriteConfig(NamedTuple):
    endpoint: str
    project_id: str
    database_id: str
    api_key: str
    source: str


DEBUG_ENABLED = os.getenv("APPWRITE_EXPORT_DEBUG", "1").strip().lower() not in {"0", "false", "no", "off"}
REDACTED_SECRET = "[REDACTED_SECRET]"
SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|auth[_-]?token|client[_-]?secret|password|"
    r"private[_-]?key|refresh[_-]?token|secret|token)($|[_-])",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b"),
)


def log_progress(message: str) -> None:
    print(f"[progress] {message}", flush=True)


def log_debug(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"[debug] {message}", flush=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    log_debug(f"Loaded required environment variable {name}")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    log_debug(f"Loaded optional environment variable {name}")
    return value


def config_from_env(prefix: str, source: str) -> AppwriteConfig | None:
    endpoint = optional_env(f"{prefix}APPWRITE_ENDPOINT")
    project_id = optional_env(f"{prefix}APPWRITE_PROJECT_ID")
    database_id = optional_env(f"{prefix}APPWRITE_DATABASE_ID")
    api_key = optional_env(f"{prefix}APPWRITE_API_KEY")
    values = (endpoint, project_id, database_id, api_key)

    if not any(values):
        return None
    if not all(values):
        missing = [
            f"{prefix}APPWRITE_ENDPOINT" if endpoint is None else "",
            f"{prefix}APPWRITE_PROJECT_ID" if project_id is None else "",
            f"{prefix}APPWRITE_DATABASE_ID" if database_id is None else "",
            f"{prefix}APPWRITE_API_KEY" if api_key is None else "",
        ]
        raise RuntimeError(f"Incomplete Appwrite configuration in {source}: {', '.join(item for item in missing if item)}")

    return AppwriteConfig(endpoint.rstrip("/"), project_id, database_id, api_key, source)


def get_appwrite_configs() -> List[AppwriteConfig]:
    configs = [
        config
        for config in (
            config_from_env("", "APPWRITE_*"),
            config_from_env("NEXT_PUBLIC_", "NEXT_PUBLIC_APPWRITE_*"),
        )
        if config is not None
    ]

    if not configs:
        raise RuntimeError(
            "Missing required Appwrite environment variables: APPWRITE_* or NEXT_PUBLIC_APPWRITE_*"
        )

    unique_configs: List[AppwriteConfig] = []
    seen = set()
    for config in configs:
        key = (config.endpoint, config.project_id, config.database_id, config.api_key)
        if key in seen:
            continue
        seen.add(key)
        unique_configs.append(config)

    return unique_configs


def appwrite_get(
    endpoint: str,
    project_id: str,
    api_key: str,
    path: str,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    query = ""
    if params:
        query = "?" + urlencode(params, doseq=True)

    url = f"{endpoint.rstrip('/')}{path}{query}"
    log_debug(f"GET {url}")

    request = Request(
        url,
        headers={
            "X-Appwrite-Project": project_id,
            "X-Appwrite-Key": api_key,
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
            size_hint = len(payload) if isinstance(payload, dict) else "n/a"
            log_debug(f"Response {response.status} from {path}; top-level keys/count hint: {size_hint}")
            return payload
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log_debug(f"HTTPError {exc.code} on {path}: {body}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None

        if exc.code == 403 and isinstance(payload, dict) and payload.get("type") == "project_paused":
            raise AppwritePausedError(payload.get("message", "Project is paused.")) from exc

        error_type = payload.get("type") if isinstance(payload, dict) else None
        raise AppwriteApiError(exc.code, path, body, error_type) from exc
    except URLError as exc:
        log_debug(f"URLError on {path}: {exc}")
        raise RuntimeError(f"Failed to reach Appwrite endpoint: {exc}") from exc


def build_query(method: str, values: List[Any], column: str | None = None) -> str:
    payload: Dict[str, Any] = {
        "method": method,
        "values": values,
    }
    if column is not None:
        payload["column"] = column
    query = json.dumps(payload, separators=(",", ":"))
    log_debug(f"Built query: {query}")
    return query


def list_collections(endpoint: str, project_id: str, api_key: str, database_id: str) -> List[Dict[str, Any]]:
    collections: List[Dict[str, Any]] = []
    page_size = 100
    cursor_after: str | None = None

    while True:
        queries = [build_query("limit", [page_size])]
        if cursor_after:
            queries.append(build_query("cursorAfter", [cursor_after]))

        payload = appwrite_get(
            endpoint,
            project_id,
            api_key,
            f"/databases/{database_id}/collections",
            params={
                "queries[]": queries,
                "total": "false",
            },
        )
        batch = payload.get("collections", [])
        collections.extend(batch)
        log_progress(f"Loaded collection page; total collections discovered: {len(collections)}")
        log_debug(f"Collection page returned {len(batch)} collections")

        if len(batch) < page_size:
            break
        cursor_after = batch[-1]["$id"]

    return collections


def list_documents(
    endpoint: str,
    project_id: str,
    api_key: str,
    database_id: str,
    collection_id: str,
    collection_name: str,
    current_index: int,
    total_collections: int,
) -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    page_size = 100
    page_number = 0
    cursor_after: str | None = None

    while True:
        page_number += 1
        queries = [build_query("limit", [page_size])]
        if cursor_after:
            queries.append(build_query("cursorAfter", [cursor_after]))

        payload = appwrite_get(
            endpoint,
            project_id,
            api_key,
            f"/databases/{database_id}/collections/{collection_id}/documents",
            params={
                "queries[]": queries,
                "total": "false",
            },
        )
        batch = payload.get("documents", [])
        documents.extend(batch)
        log_progress(
            f"[{current_index}/{total_collections}] {collection_name} ({collection_id}) page {page_number}: "
            f"fetched {len(batch)} docs, accumulated {len(documents)}"
        )
        log_debug(
            f"Collection {collection_name} ({collection_id}) page {page_number} used cursor pagination"
        )

        if len(batch) < page_size:
            break
        cursor_after = batch[-1]["$id"]

    return documents


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_debug(f"Wrote JSON file to {path.resolve()}")


def redact_string(value: str, key_hint: str | None = None) -> str:
    redacted = value
    if key_hint and SENSITIVE_KEY_PATTERN.search(key_hint):
        return REDACTED_SECRET

    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED_SECRET, redacted)

    return redacted


def sanitize_payload(value: Any, key_hint: str | None = None) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_payload(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, key_hint) for item in value]
    if isinstance(value, str):
        return redact_string(value, key_hint)
    return value


def build_snapshot_with_config(config: AppwriteConfig) -> Dict[str, Any]:
    log_progress(f"Starting Appwrite export for database {config.database_id} using {config.source}")
    log_debug(f"Debug logging is {'enabled' if DEBUG_ENABLED else 'disabled'}")
    log_debug(f"Export endpoint: {config.endpoint}")
    log_debug(f"Export project ID: {config.project_id}")

    collections = list_collections(config.endpoint, config.project_id, config.api_key, config.database_id)
    exported_collections = []
    total_collections = len(collections)
    log_progress(f"Discovered {total_collections} collections to export")

    for index, collection in enumerate(collections, start=1):
        collection_id = collection["$id"]
        collection_name = collection.get("name") or collection_id
        log_progress(f"[{index}/{total_collections}] Exporting collection {collection_name} ({collection_id})")
        log_debug(
            f"Collection metadata for {collection_name} ({collection_id}): permissions={len(collection.get('$permissions', []))}"
        )
        documents = list_documents(
            config.endpoint,
            config.project_id,
            config.api_key,
            config.database_id,
            collection_id,
            collection_name,
            index,
            total_collections,
        )
        exported_collections.append(
            {
                "collection": collection,
                "documentsCount": len(documents),
                "documents": documents,
            }
        )
        log_progress(
            f"[{index}/{total_collections}] Completed {collection_name} ({collection_id}) with {len(documents)} documents"
        )
        log_debug(f"Snapshot now contains {len(exported_collections)} exported collections")

    exported_at = datetime.now(timezone.utc)
    log_debug(f"Snapshot timestamp: {exported_at.isoformat()}")
    snapshot = {
        "exportedAt": exported_at.isoformat(),
        "projectId": config.project_id,
        "databaseId": config.database_id,
        "collectionCount": len(exported_collections),
        "collections": exported_collections,
    }
    sanitized_snapshot = sanitize_payload(snapshot)
    if sanitized_snapshot != snapshot:
        log_progress("Redacted sensitive values from exported snapshot")
    return sanitized_snapshot


def build_snapshot() -> Dict[str, Any]:
    configs = get_appwrite_configs()
    last_project_not_found: AppwriteApiError | None = None

    for index, config in enumerate(configs, start=1):
        try:
            return build_snapshot_with_config(config)
        except AppwriteApiError as exc:
            if exc.error_type == "project_not_found" and index < len(configs):
                last_project_not_found = exc
                log_progress(
                    f"{config.source} returned project_not_found; trying alternate Appwrite configuration"
                )
                continue
            raise

    if last_project_not_found:
        raise RuntimeError(
            "Every Appwrite configuration returned project_not_found. Check that the endpoint and project ID belong "
            "to the same Appwrite project in GitHub Secrets."
        ) from last_project_not_found

    raise RuntimeError("No Appwrite configuration was available")


def main() -> int:
    try:
        snapshot = build_snapshot()
        exported_at = datetime.fromisoformat(snapshot["exportedAt"])
        stamp = exported_at.strftime("%Y%m%dT%H%M%SZ")

        base_dir = Path("data/appwrite")
        latest_path = base_dir / "latest.json"
        history_path = base_dir / "history" / f"snapshot-{stamp}.json"

        log_debug(f"Preparing to write latest snapshot to {latest_path}")
        log_debug(f"Preparing to write history snapshot to {history_path}")
        write_json(latest_path, snapshot)
        write_json(history_path, snapshot)

        print(f"Exported {snapshot['collectionCount']} collections to {latest_path} and {history_path}")
        return 0
    except AppwritePausedError as exc:
        print(
            "::warning::Appwrite project is paused, so this backup run was skipped. "
            f"Restore the project in Appwrite Console to resume exports. Details: {exc}"
        )
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
