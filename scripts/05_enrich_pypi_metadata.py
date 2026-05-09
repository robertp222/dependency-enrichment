import argparse
import datetime
import json
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "db" / "libraries.db"
PYPI_URL = "https://pypi.org/pypi"

load_dotenv()


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


def is_valid_python_package_name(name):
    if not name:
        return False

    if " " in name or "\t" in name or "\n" in name:
        return False

    return bool(name) and all(ch.isalnum() or ch in {"-", "_", "."} for ch in name)


def validate_python_package_name(name):
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

    if not is_valid_python_package_name(normalized):
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
        return not enrichment_status
    return enrichment_status != "success"


def fetch_pypi_metadata(package_name):
    safe_name = quote(package_name, safe="")
    url = f"{PYPI_URL}/{safe_name}/json"
    request = Request(url)

    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except URLError:
        raise


def normalize_keywords(keywords):
    if not keywords:
        return ""
    if isinstance(keywords, str):
        return keywords.strip()
    if isinstance(keywords, list):
        return ",".join(str(item).strip() for item in keywords if item)
    return str(keywords).strip()


def build_metadata(package_name, pypi_data):
    if pypi_data is None:
        return None

    info = pypi_data.get("info") or {}
    latest = str(info.get("version", "") or "").strip()
    homepage = str(info.get("home_page", "") or "").strip()
    repository_url = ""
    project_urls = info.get("project_urls") or {}

    if isinstance(project_urls, dict):
        repository_url = project_urls.get("Source", "") or project_urls.get("Homepage", "") or ""

    if not repository_url:
        repository_url = homepage

    keywords = info.get("keywords")
    if isinstance(keywords, str):
        normalized_keywords = keywords.strip()
    elif isinstance(keywords, list):
        normalized_keywords = ",".join(str(item).strip() for item in keywords if item)
    else:
        normalized_keywords = ""

    description = str(info.get("summary", "") or "").strip()
    if not description:
        description = str(info.get("description", "") or "").strip()

    return {
        "description": description,
        "homepage": homepage,
        "repository_url": repository_url,
        "keywords": normalize_keywords(normalized_keywords),
        "latest_version": latest,
        "enrichment_status": "success",
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich Python package metadata from the public PyPI registry.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit to the number of python packages to enrich.")
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
        "SELECT library_id, name, enrichment_status FROM library_catalog WHERE type = 'python'"
    )
    python_rows = cursor.fetchall()

    filtered_rows = [row for row in python_rows if should_include_row(row[2], args)]

    if args.limit is not None:
        filtered_rows = filtered_rows[: args.limit]

    remaining = len(filtered_rows)
    if remaining == 0:
        print("No python libraries found to enrich.")
        return

    print(f"Found {len(python_rows)} python libraries total.")
    print(f"Remaining to process: {remaining}")
    if args.limit is not None:
        print(f"Limiting enrichment to {args.limit} python libraries.")
    if args.force:
        print("Force mode enabled: re-enriching all selected python rows.")
    if args.only_missing:
        print("Only missing mode enabled: enriching only rows with no existing status.")

    stats = {"processed": 0, "success": 0, "not_found": 0, "skipped": 0, "error": 0}

    for index, (library_id, name, _) in enumerate(filtered_rows, start=1):
        stats["processed"] += 1
        normalized_name = normalize_name(name)
        validation = validate_python_package_name(normalized_name)

        cursor.execute(
            "UPDATE library_catalog SET validation_status = ?, skip_reason = ? WHERE library_id = ?",
            (validation["status"], validation["reason"], library_id),
        )

        if validation["status"] != "valid":
            stats["skipped"] += 1
            if index % 100 == 0 or index == remaining:
                print(f"Processed {index}/{remaining} python libraries.")
                conn.commit()
            time.sleep(0.2)
            continue

        try:
            pypi_data = fetch_pypi_metadata(normalized_name)
            metadata = build_metadata(normalized_name, pypi_data)
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
        except Exception as exc:
            timestamp = datetime.datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE library_catalog SET enrichment_status = ?, enriched_at = ? WHERE library_id = ?",
                ("error", timestamp, library_id),
            )
            stats["error"] += 1
            print(f"Error fetching {normalized_name}: {exc}")

        if index % 100 == 0 or index == remaining:
            print(f"Processed {index}/{remaining} python libraries.")
            conn.commit()

        time.sleep(0.2)

    print("--- Enrichment complete ---")
    print(f"Total python libraries: {len(python_rows)}")
    print(f"Remaining: {remaining}")
    print(f"Processed: {stats['processed']}")
    print(f"Success: {stats['success']}")
    print(f"Not found: {stats['not_found']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Error: {stats['error']}")
    conn.close()


if __name__ == "__main__":
    main()
