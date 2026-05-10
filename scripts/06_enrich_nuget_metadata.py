import argparse
import datetime
import json
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "db" / "libraries.db"
NUGET_API_URL = "https://api.nuget.org/v3-flatcontainer"


def normalize_name(name):
    if name is None:
        return ""
    return str(name).strip()


def is_local_path(name):
    return name.startswith("./") or name.startswith("../") or name.startswith("/")


def is_file_link_workspace(name):
    lower = name.lower()
    return lower.startswith("file:") or lower.startswith("link:") or lower.startswith("workspace:")


def is_github_reference(name):
    lower = name.lower()
    return lower.startswith("github:") or "github.com/" in lower


def is_git_reference(name):
    lower = name.lower()
    return (
        lower.startswith("git+")
        or lower.startswith("git://")
        or lower.startswith("ssh://")
        or lower.endswith(".git")
    )


def is_valid_nuget_package_name(name):
    if not name:
        return False

    if " " in name or "\t" in name or "\n" in name:
        return False

    return bool(name) and all(ch.isalnum() or ch in {"-", "_", "."} for ch in name)


def is_internal_artifact(name):
    """Check if the name looks like an internal assembly or project artifact."""
    lower = name.lower()
    
    # File extensions indicating assemblies or source files
    if lower.endswith('.dll') or lower.endswith('.exe') or lower.endswith('.cs') or lower.endswith('.vb'):
        return True
    
    # Designer files
    if 'designer' in lower:
        return True
    
    # Names that look like version numbers (e.g., "1.2.3.4")
    import re
    if re.match(r'^\d+(\.\d+)+$', name):
        return True
    
    # Names starting with dot
    if name.startswith('.'):
        return True
    
    # Names that look like internal project artifacts
    # Multiple dots often indicate internal namespaces
    if name.count('.') > 3:
        return True
    
    # Names starting with common internal prefixes
    internal_prefixes = [
        'system.', 'microsoft.', 'windows.', 'mono.', 'mono.', 'xamarin.',
        'abb.', 'afs.', 'access.', 'business', 'data.', 'ui.', 'web.',
        'internal.', 'private.', 'local.', 'temp.', 'test.'
    ]
    for prefix in internal_prefixes:
        if lower.startswith(prefix):
            return True
    
    return False


def validate_nuget_package_name(name):
    normalized = normalize_name(name)
    if not normalized:
        return {"status": "invalid", "reason": "empty name"}

    if is_local_path(normalized):
        return {"status": "skipped", "reason": "local path"}

    if is_file_link_workspace(normalized):
        return {"status": "skipped", "reason": "unsupported protocol"}

    if is_github_reference(normalized):
        return {"status": "skipped", "reason": "github url"}

    if is_git_reference(normalized):
        return {"status": "skipped", "reason": "git url"}

    if is_internal_artifact(normalized):
        return {"status": "internal", "reason": "internal artifact or assembly"}

    if not is_valid_nuget_package_name(normalized):
        return {"status": "invalid", "reason": "invalid package name"}

    return {"status": "valid", "reason": ""}


def ensure_columns(cursor):
    cursor.execute("PRAGMA table_info(library_catalog)")
    columns = {row[1] for row in cursor.fetchall()}

    needed = [
        ("description", "TEXT", "''"),
        ("homepage", "TEXT", "''"),
        ("repository_url", "TEXT", "''"),
        ("keywords", "TEXT", "''"),
        ("latest_version", "TEXT", "''"),
        ("enrichment_status", "TEXT", "''"),
        ("enriched_at", "TEXT", "''"),
        ("validation_status", "TEXT", "''"),
        ("skip_reason", "TEXT", "''"),
    ]

    for name, column_type, default in needed:
        if name not in columns:
            cursor.execute(
                f"ALTER TABLE library_catalog ADD COLUMN {name} {column_type} DEFAULT {default}"
            )


def should_include_row(enrichment_status, args):
    if args.force:
        return True
    if args.only_missing:
        return not enrichment_status or enrichment_status in ("", "internal", "retry_later")
    return enrichment_status not in ("success", "internal")


def fetch_nuget_metadata(package_name):
    # Use NuGet search API to get package metadata
    safe_name = quote(package_name, safe="")
    url = f"https://azuresearch-usnc.nuget.org/query?q={safe_name}&take=1&prerelease=false"
    
    max_retries = 3
    base_delay = 1.0  # seconds
    
    for attempt in range(max_retries):
        try:
            request = Request(url)
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except HTTPError as exc:
            if exc.code == 404:
                return None  # Real not found
            elif exc.code == 429 or exc.code >= 500:
                # Retry for rate limit or server errors
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                    continue
                else:
                    raise  # Max retries exceeded
            else:
                raise  # Other HTTP errors
        except URLError as exc:
            # Retry for network/SSL timeouts
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            else:
                raise  # Max retries exceeded


def normalize_keywords(keywords):
    if not keywords:
        return ""
    if isinstance(keywords, str):
        return keywords.strip()
    if isinstance(keywords, list):
        return ",".join(str(item).strip() for item in keywords if item)
    return str(keywords).strip()


def build_metadata(package_name, nuget_data):
    if nuget_data is None or "data" not in nuget_data or not nuget_data["data"]:
        return None

    package_data = nuget_data["data"][0]

    description = str(package_data.get("description", "") or "").strip()
    homepage = str(package_data.get("projectUrl", "") or "").strip()
    repository_url = ""  # Search API doesn't include repository URL directly
    tags = normalize_keywords(package_data.get("tags", []))
    latest_version = str(package_data.get("version", "") or "").strip()

    return {
        "description": description,
        "homepage": homepage,
        "repository_url": repository_url,
        "keywords": tags,
        "latest_version": latest_version,
        "enrichment_status": "success",
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich .NET package metadata from the public NuGet registry.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit to the number of dotnet packages to enrich.")
    parser.add_argument("--force", action="store_true", help="Re-enrich packages even if previously enriched successfully.")
    parser.add_argument("--only-missing", action="store_true", help="Process only rows without an existing enrichment status.")
    return parser.parse_args()


def main():
    args = parse_args()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    ensure_columns(cursor)
    conn.commit()

    cursor.execute(
        "SELECT library_id, name, enrichment_status FROM library_catalog WHERE type = 'dotnet' ORDER BY library_id"
    )
    dotnet_rows = cursor.fetchall()

    filtered_rows = [row for row in dotnet_rows if should_include_row(row[2], args)]

    if args.limit is not None:
        filtered_rows = filtered_rows[: args.limit]

    remaining = len(filtered_rows)
    if remaining == 0:
        print("No dotnet libraries found to enrich.")
        return

    print(f"Found {len(dotnet_rows)} dotnet libraries total.")
    print(f"Remaining to process: {remaining}")
    if args.limit is not None:
        print(f"Limiting enrichment to {args.limit} dotnet libraries.")
    if args.force:
        print("Force mode enabled: re-enriching all selected dotnet rows.")
    if args.only_missing:
        print("Only missing mode enabled: enriching only rows with no existing status.")

    stats = {"processed": 0, "success": 0, "not_found": 0, "internal": 0, "skipped": 0, "error": 0, "retry_later": 0}

    for index, (library_id, name, _) in enumerate(filtered_rows, start=1):
        stats["processed"] += 1
        normalized_name = normalize_name(name)
        validation = validate_nuget_package_name(normalized_name)

        cursor.execute(
            "UPDATE library_catalog SET validation_status = ?, skip_reason = ? WHERE library_id = ?",
            (validation["status"], validation["reason"], library_id),
        )

        if validation["status"] == "internal":
            cursor.execute(
                "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                ("internal", datetime.datetime.utcnow().isoformat(), library_id),
            )
            stats["internal"] += 1
            if index % 100 == 0 or index == remaining:
                print(f"Processed {index}/{remaining} dotnet libraries.")
                conn.commit()
            time.sleep(0.2)
            continue
        elif validation["status"] != "valid":
            stats["skipped"] += 1
            if index % 100 == 0 or index == remaining:
                print(f"Processed {index}/{remaining} dotnet libraries.")
                conn.commit()
            time.sleep(0.2)
            continue

        try:
            nuget_data = fetch_nuget_metadata(normalized_name)
            metadata = build_metadata(normalized_name, nuget_data)
            timestamp = datetime.datetime.utcnow().isoformat()

            if metadata is None:
                cursor.execute(
                    "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                    ("not_found", timestamp, library_id),
                )
                stats["not_found"] += 1
            else:
                cursor.execute(
                    "UPDATE library_catalog SET description = ?, homepage = ?, repository_url = ?, keywords = ?, latest_version = ?, enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                    (
                        metadata["description"],
                        metadata["homepage"],
                        metadata["repository_url"],
                        metadata["keywords"],
                        metadata["latest_version"],
                        metadata["enrichment_status"],
                        timestamp,
                        library_id,
                    ),
                )
                stats["success"] += 1
        except URLError as exc:
            # Network/SSL timeout after retries
            timestamp = datetime.datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                ("retry_later", timestamp, library_id),
            )
            stats["retry_later"] += 1
            print(f"Timeout after retries for {normalized_name}: {exc}")
        except HTTPError as exc:
            # HTTP error after retries (not 404, since that's handled in fetch_nuget_metadata)
            timestamp = datetime.datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                ("error", timestamp, library_id),
            )
            stats["error"] += 1
            print(f"HTTP error after retries for {normalized_name}: {exc.code}")
        except Exception as exc:
            timestamp = datetime.datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                ("error", timestamp, library_id),
            )
            stats["error"] += 1
            print(f"Unexpected error fetching {normalized_name}: {exc}")

        if index % 100 == 0 or index == remaining:
            print(f"Processed {index}/{remaining} dotnet libraries.")
            conn.commit()

        time.sleep(0.2)

    print("--- Enrichment complete ---")
    print(f"Total dotnet libraries: {len(dotnet_rows)}")
    print(f"Remaining: {remaining}")
    print(f"Processed: {stats['processed']}")
    print(f"Success: {stats['success']}")
    print(f"Not found: {stats['not_found']}")
    print(f"Internal: {stats['internal']}")
    print(f"Retry later: {stats['retry_later']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Error: {stats['error']}")
    conn.close()


if __name__ == "__main__":
    main()
