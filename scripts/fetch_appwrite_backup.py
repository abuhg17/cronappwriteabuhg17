#!/usr/bin/env python3
"""Export Appwrite database collections/documents to versioned JSON snapshots.

Architecture (high level)
-------------------------
1. Resolve credentials from APPWRITE_* or NEXT_PUBLIC_APPWRITE_* env vars.
2. Paginate every collection + every document via the Appwrite REST API.
3. Sanitize secrets out of the payload before writing to git.
4. Compare the new export to data/appwrite/latest.json (ignoring exportedAt).
   - Unchanged → skip write/commit noise; still prune history if needed.
   - Changed → write latest.json + a timestamped history snapshot.
5. landtophistory.json by UTC hour parity:
   - Odd hours (1,3,…,23) → write full snapshot to landtophistory.json
   - Even hours (0,2,…,22) → remove landtophistory.json if present
6. Enforce HISTORY_KEEP_COUNT so the repo does not grow without bound.

No third-party deps: stdlib urllib + json only (CI-friendly).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppwritePausedError(RuntimeError):
    """Appwrite paused the project (usually inactivity). Safe to skip the run."""


class AppwriteApiError(RuntimeError):
    def __init__(
        self,
        code: int,
        path: str,
        body: str,
        error_type: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.body = body
        self.error_type = error_type
        super().__init__(f"Appwrite API error {code} on {path}: {body}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppwriteConfig:
    endpoint: str
    project_id: str
    database_id: str
    api_key: str
    source: str


DEBUG_ENABLED = os.getenv("APPWRITE_EXPORT_DEBUG", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# How many history snapshots to keep (oldest deleted after each successful export).
# Default 336 ≈ 7 days if the job runs twice per hour, or ~14 days if once/hour.
HISTORY_KEEP_COUNT = max(1, int(os.getenv("APPWRITE_HISTORY_KEEP_COUNT", "336")))

PAGE_SIZE = max(1, min(100, int(os.getenv("APPWRITE_PAGE_SIZE", "100"))))
HTTP_TIMEOUT_SEC = max(5, int(os.getenv("APPWRITE_HTTP_TIMEOUT", "60")))
HTTP_RETRIES = max(0, int(os.getenv("APPWRITE_HTTP_RETRIES", "2")))

BASE_DIR = Path(os.getenv("APPWRITE_EXPORT_DIR", "data/appwrite"))
LATEST_PATH = BASE_DIR / "latest.json"
HISTORY_DIR = BASE_DIR / "history"
# Odd UTC hours → write this snapshot; even UTC hours → delete it.
LANDTOPHISTORY_PATH = BASE_DIR / "landtophistory.json"

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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_progress(message: str) -> None:
    print(f"[progress] {message}", flush=True)


def log_debug(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"[debug] {message}", flush=True)


# ---------------------------------------------------------------------------
# Environment / config resolution
# ---------------------------------------------------------------------------


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    log_debug(f"Loaded env {name}")
    return value


def config_from_env(prefix: str, source: str) -> AppwriteConfig | None:
    suffix_map = {
        "endpoint": "ENDPOINT",
        "project_id": "PROJECT_ID",
        "database_id": "DATABASE_ID",
        "api_key": "API_KEY",
    }
    fields = {
        key: optional_env(f"{prefix}APPWRITE_{suffix}")
        for key, suffix in suffix_map.items()
    }
    if not any(fields.values()):
        return None
    missing = [
        f"{prefix}APPWRITE_{suffix_map[key]}"
        for key, value in fields.items()
        if value is None
    ]
    if missing:
        raise RuntimeError(
            f"Incomplete Appwrite configuration in {source}: {', '.join(missing)}"
        )
    return AppwriteConfig(
        endpoint=str(fields["endpoint"]).rstrip("/"),
        project_id=str(fields["project_id"]),
        database_id=str(fields["database_id"]),
        api_key=str(fields["api_key"]),
        source=source,
    )


def get_appwrite_configs() -> list[AppwriteConfig]:
    """Prefer APPWRITE_*; fall back to NEXT_PUBLIC_APPWRITE_* (deduped)."""
    candidates = [
        config_from_env("", "APPWRITE_*"),
        config_from_env("NEXT_PUBLIC_", "NEXT_PUBLIC_APPWRITE_*"),
    ]
    configs = [c for c in candidates if c is not None]
    if not configs:
        raise RuntimeError(
            "Missing required Appwrite environment variables: "
            "APPWRITE_* or NEXT_PUBLIC_APPWRITE_*"
        )

    unique: list[AppwriteConfig] = []
    seen: set[tuple[str, str, str, str]] = set()
    for config in configs:
        key = (config.endpoint, config.project_id, config.database_id, config.api_key)
        if key in seen:
            continue
        seen.add(key)
        unique.append(config)
    return unique


# ---------------------------------------------------------------------------
# Sanitization (never commit raw secrets into the repo)
# ---------------------------------------------------------------------------


def redact_string(value: str, key_hint: str | None = None) -> str:
    if key_hint and SENSITIVE_KEY_PATTERN.search(key_hint):
        return REDACTED_SECRET
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED_SECRET, redacted)
    return redacted


def sanitize_payload(value: Any, key_hint: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: sanitize_payload(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, key_hint) for item in value]
    if isinstance(value, str):
        return redact_string(value, key_hint)
    return value


# ---------------------------------------------------------------------------
# Change detection helpers
# ---------------------------------------------------------------------------


def content_fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable hash of export payload excluding volatile timestamps."""
    comparable = {
        "projectId": snapshot.get("projectId"),
        "databaseId": snapshot.get("databaseId"),
        "collectionCount": snapshot.get("collectionCount"),
        "collections": snapshot.get("collections"),
    }
    raw = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log_progress(f"Could not read {path}: {exc}")
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Appwrite HTTP client
# ---------------------------------------------------------------------------


def build_query(method: str, values: list[Any], column: str | None = None) -> str:
    payload: dict[str, Any] = {"method": method, "values": values}
    if column is not None:
        payload["column"] = column
    return json.dumps(payload, separators=(",", ":"))


class AppwriteClient:
    """Thin REST client with cursor pagination."""

    def __init__(self, config: AppwriteConfig) -> None:
        self.config = config

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.config.endpoint.rstrip('/')}{path}{query}"
        log_debug(f"GET {url}")

        request = Request(
            url,
            headers={
                "X-Appwrite-Project": self.config.project_id,
                "X-Appwrite-Key": self.config.api_key,
                "Content-Type": "application/json",
            },
            method="GET",
        )

        last_error: Exception | None = None
        attempts = HTTP_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    log_debug(f"Response {response.status} from {path}")
                    return payload
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                log_debug(f"HTTPError {exc.code} on {path}: {body}")
                try:
                    err_payload = json.loads(body)
                except json.JSONDecodeError:
                    err_payload = None

                if (
                    exc.code == 403
                    and isinstance(err_payload, dict)
                    and err_payload.get("type") == "project_paused"
                ):
                    raise AppwritePausedError(
                        err_payload.get("message", "Project is paused.")
                    ) from exc

                # Retry only on transient 5xx / 429
                if exc.code in {429, 500, 502, 503, 504} and attempt < attempts:
                    log_progress(
                        f"Transient HTTP {exc.code} on {path}; retry {attempt}/{attempts - 1}"
                    )
                    last_error = exc
                    continue

                error_type = err_payload.get("type") if isinstance(err_payload, dict) else None
                raise AppwriteApiError(exc.code, path, body, error_type) from exc
            except URLError as exc:
                log_debug(f"URLError on {path}: {exc}")
                if attempt < attempts:
                    log_progress(f"Network error on {path}; retry {attempt}/{attempts - 1}")
                    last_error = exc
                    continue
                raise RuntimeError(f"Failed to reach Appwrite endpoint: {exc}") from exc

        raise RuntimeError(f"Failed after retries on {path}: {last_error}")

    def paginate(
        self,
        path: str,
        list_key: str,
        on_page: Callable[[list[dict[str, Any]], int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Cursor-after pagination until a short page is returned."""
        items: list[dict[str, Any]] = []
        cursor_after: str | None = None
        page_number = 0

        while True:
            page_number += 1
            queries = [build_query("limit", [PAGE_SIZE])]
            if cursor_after:
                queries.append(build_query("cursorAfter", [cursor_after]))

            payload = self.get(
                path,
                params={"queries[]": queries, "total": "false"},
            )
            batch = payload.get(list_key, [])
            if not isinstance(batch, list):
                raise RuntimeError(f"Unexpected {list_key} payload from {path}")

            items.extend(batch)
            if on_page:
                on_page(batch, page_number)

            if len(batch) < PAGE_SIZE:
                break
            cursor_after = batch[-1]["$id"]

        return items

    def list_collections(self) -> list[dict[str, Any]]:
        total = 0

        def on_page(batch: list[dict[str, Any]], _page: int) -> None:
            nonlocal total
            total += len(batch)
            log_progress(f"Loaded collection page (+{len(batch)}); total: {total}")

        collections = self.paginate(
            f"/databases/{self.config.database_id}/collections",
            "collections",
            on_page=on_page,
        )
        log_progress(f"Discovered {len(collections)} collections")
        return collections

    def list_documents(
        self,
        collection_id: str,
        collection_name: str,
        index: int,
        total: int,
    ) -> list[dict[str, Any]]:
        db = self.config.database_id
        path = f"/databases/{db}/collections/{collection_id}/documents"
        accumulated = 0

        def on_page(batch: list[dict[str, Any]], page_number: int) -> None:
            nonlocal accumulated
            accumulated += len(batch)
            log_progress(
                f"[{index}/{total}] {collection_name} ({collection_id}) "
                f"page {page_number}: +{len(batch)} docs, total {accumulated}"
            )

        return self.paginate(path, "documents", on_page=on_page)


# ---------------------------------------------------------------------------
# Snapshot build
# ---------------------------------------------------------------------------


def build_snapshot_with_config(config: AppwriteConfig) -> dict[str, Any]:
    client = AppwriteClient(config)
    log_progress(
        f"Starting export database={config.database_id} via {config.source}"
    )
    log_debug(f"endpoint={config.endpoint} project={config.project_id}")

    collections = client.list_collections()
    total = len(collections)
    exported: list[dict[str, Any]] = []

    for index, collection in enumerate(collections, start=1):
        collection_id = collection["$id"]
        collection_name = collection.get("name") or collection_id
        log_progress(
            f"[{index}/{total}] Exporting {collection_name} ({collection_id})"
        )
        documents = client.list_documents(
            collection_id, collection_name, index, total
        )
        exported.append(
            {
                "collection": collection,
                "documentsCount": len(documents),
                "documents": documents,
            }
        )
        log_progress(
            f"[{index}/{total}] Done {collection_name}: {len(documents)} documents"
        )

    exported_at = datetime.now(timezone.utc)
    snapshot = {
        "exportedAt": exported_at.isoformat(),
        "projectId": config.project_id,
        "databaseId": config.database_id,
        "collectionCount": len(exported),
        "contentHash": "",  # filled after sanitize
        "collections": exported,
    }
    sanitized = sanitize_payload(snapshot)
    if sanitized != snapshot:
        log_progress("Redacted sensitive values from exported snapshot")

    # Fingerprint after sanitize so committed files are self-describing
    fingerprint = content_fingerprint(sanitized)
    sanitized["contentHash"] = fingerprint
    log_debug(f"contentHash={fingerprint}")
    return sanitized


def build_snapshot() -> dict[str, Any]:
    """Try each config; only project_not_found falls through to the next."""
    configs = get_appwrite_configs()
    last_not_found: AppwriteApiError | None = None

    for index, config in enumerate(configs, start=1):
        try:
            return build_snapshot_with_config(config)
        except AppwriteApiError as exc:
            if exc.error_type == "project_not_found":
                last_not_found = exc
                if index < len(configs):
                    log_progress(
                        f"{config.source} → project_not_found; trying alternate config"
                    )
                    continue
            raise

    if last_not_found:
        raise RuntimeError(
            "Every Appwrite configuration returned project_not_found. "
            "Check that endpoint and project ID belong to the same Appwrite project."
        ) from last_not_found
    raise RuntimeError("No Appwrite configuration was available")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log_debug(f"Wrote {path.resolve()}")


def prune_history(keep: int = HISTORY_KEEP_COUNT) -> int:
    """Delete oldest snapshot-*.json files beyond `keep`. Returns deleted count."""
    if not HISTORY_DIR.is_dir():
        return 0
    snapshots = sorted(HISTORY_DIR.glob("snapshot-*.json"))
    excess = len(snapshots) - keep
    if excess <= 0:
        return 0
    deleted = 0
    for path in snapshots[:excess]:
        path.unlink(missing_ok=True)
        deleted += 1
    if deleted:
        log_progress(f"Pruned {deleted} old history snapshots (keep={keep})")
    return deleted


def is_odd_utc_hour(when: datetime | None = None) -> bool:
    """True for UTC hours 1,3,5,…,23; False for 0,2,4,…,22."""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.hour % 2 == 1


def apply_landtophistory(snapshot: dict[str, Any], when: datetime | None = None) -> str:
    """Odd UTC hour → write landtophistory.json; even UTC hour → remove it.

    Returns action label: "wrote" | "removed" | "absent".
    """
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)

    hour = moment.hour
    if is_odd_utc_hour(moment):
        payload = {
            **snapshot,
            "landtophistory": True,
            "landtophistoryUtcHour": hour,
            "landtophistoryMode": "odd-hour-write",
        }
        write_json(LANDTOPHISTORY_PATH, payload)
        log_progress(
            f"Odd UTC hour {hour:02d} → wrote {LANDTOPHISTORY_PATH}"
        )
        return "wrote"

    if LANDTOPHISTORY_PATH.is_file():
        LANDTOPHISTORY_PATH.unlink()
        log_progress(
            f"Even UTC hour {hour:02d} → removed {LANDTOPHISTORY_PATH}"
        )
        return "removed"

    log_progress(
        f"Even UTC hour {hour:02d} → landtophistory already absent"
    )
    return "absent"


def persist_snapshot(snapshot: dict[str, Any]) -> tuple[bool, int, str]:
    """Write latest + history when content changed; toggle landtophistory by hour.

    Returns (wrote_new_snapshot, pruned_count, landtophistory_action).
    """
    previous = load_json(LATEST_PATH)
    new_hash = snapshot.get("contentHash") or content_fingerprint(snapshot)
    old_hash = None
    if previous is not None:
        old_hash = previous.get("contentHash") or content_fingerprint(previous)

    wrote = False
    if old_hash == new_hash and previous is not None:
        log_progress(
            f"No data changes (contentHash={new_hash[:12]}…); skipping snapshot write"
        )
    else:
        exported_at = datetime.fromisoformat(snapshot["exportedAt"])
        stamp = exported_at.strftime("%Y%m%dT%H%M%SZ")
        history_path = HISTORY_DIR / f"snapshot-{stamp}.json"
        write_json(LATEST_PATH, snapshot)
        write_json(history_path, snapshot)
        wrote = True
        log_progress(
            f"Wrote latest + history ({snapshot['collectionCount']} collections) → {history_path}"
        )

    pruned = prune_history()
    land_action = apply_landtophistory(snapshot)
    return wrote, pruned, land_action


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        now = datetime.now(timezone.utc)
        log_debug(
            f"settings page_size={PAGE_SIZE} retries={HTTP_RETRIES} "
            f"history_keep={HISTORY_KEEP_COUNT} debug={DEBUG_ENABLED} "
            f"utc_hour={now.hour:02d} landtophistory="
            f"{'write' if is_odd_utc_hour(now) else 'remove'}"
        )
        snapshot = build_snapshot()
        wrote, pruned, land_action = persist_snapshot(snapshot)
        if wrote:
            print(
                f"Exported {snapshot['collectionCount']} collections "
                f"(hash={snapshot.get('contentHash', '')[:12]}…) "
                f"to {LATEST_PATH}"
            )
        else:
            print(
                f"Export unchanged "
                f"(hash={snapshot.get('contentHash', '')[:12]}…); "
                f"no new snapshot files"
            )
        if pruned:
            print(f"Pruned {pruned} history files (keep={HISTORY_KEEP_COUNT})")
        print(
            f"landtophistory: {land_action} "
            f"(UTC hour {now.hour:02d}, "
            f"{'odd→write' if is_odd_utc_hour(now) else 'even→remove'})"
        )
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
