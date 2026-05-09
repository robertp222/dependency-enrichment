import argparse
import datetime
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
RULES_DIR = BASE_DIR / "rules"
DB_FILE = BASE_DIR / "db" / "libraries.db"
TAGS_TAXONOMY_FILE = RULES_DIR / "tags_taxonomy.yaml"

load_dotenv()


def parse_simple_yaml(text):
    """Parse simple YAML structure to extract tag groups."""
    items = {}
    current_group = None
    in_tag_groups = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        if "tag_groups:" in line:
            in_tag_groups = True
            continue

        if not in_tag_groups:
            continue

        if line.startswith("  ") and not line.startswith("    "):
            stripped = line.strip()
            if stripped and ":" in stripped and not stripped.startswith("-"):
                key, value = stripped.split(":", 1)
                current_group = key.strip()
                items[current_group] = []
        elif line.startswith("    -"):
            if current_group:
                tag = line.strip()[1:].strip()
                items[current_group].append(tag)

    return items


def load_tags_taxonomy(path):
    """Load allowed tags from taxonomy YAML."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    tag_groups = parse_simple_yaml(text)

    all_tags = set()
    for group_name, tags in tag_groups.items():
        if isinstance(tags, list):
            all_tags.update(tags)

    return all_tags, tag_groups


def ensure_columns(cursor):
    """Add OpenAI-related columns if they don't exist."""
    cursor.execute("PRAGMA table_info(library_catalog)")
    columns = {row[1] for row in cursor.fetchall()}

    needed = [
        ("short_description", "TEXT", "''"),
        ("ai_model", "TEXT", "''"),
        ("ai_classified_at", "TEXT", "''"),
    ]

    for name, column_type, default in needed:
        if name not in columns:
            cursor.execute(
                f"ALTER TABLE library_catalog ADD COLUMN {name} {column_type} DEFAULT {default}"
            )


def query_unclassified(cursor, args):
    """Query libraries that need OpenAI classification."""
    where_clause = """
        WHERE type = 'npm'
        AND (technology IS NULL OR technology = '')
        AND (description IS NOT NULL AND description != '')
    """
    if args.only_missing:
        where_clause += " AND (classification_source IS NULL OR classification_source = '')"

    query = f"SELECT library_id, name, type, description, keywords, homepage, repository_url FROM library_catalog {where_clause}"
    cursor.execute(query)

    rows = cursor.fetchall()
    if args.limit:
        rows = rows[: args.limit]

    return rows


def build_openai_prompt(record, all_tags, tag_groups):
    """Build structured prompt for OpenAI with taxonomy constraints."""
    library_id, name, lib_type, description, keywords, homepage, repo_url = record

    tag_groups_text = "\n".join(
        [f"  - {group}: {', '.join(tags)}" for group, tags in tag_groups.items()]
    )

    prompt = f"""Classify this software library based on the provided metadata.

Library: {name}
Type: {lib_type}
Description: {description}
Keywords: {keywords if keywords else "N/A"}
Homepage: {homepage if homepage else "N/A"}
Repository: {repo_url if repo_url else "N/A"}

Available tag groups and values:
{tag_groups_text}

Respond with ONLY a valid JSON object (no markdown, no extra text):
{{
  "technology": "canonical technology family name (e.g., React, Django, PostgreSQL)",
  "tags": ["tag1", "tag2", "tag3"],
  "short_description": "one-line description of the library's purpose",
  "confidence": 0.95
}}

Rules:
- technology: main product/framework family name
- tags: select ONLY from the available tags above, use 2-5 tags
- confidence: 0.0 to 1.0, how confident is this classification
- Do not invent tags outside the provided taxonomy
"""
    return prompt


def call_openai_api(prompt):
    """Call OpenAI API and return response."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai package required: pip install openai")

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=300,
    )

    return response.choices[0].message.content.strip()


def parse_openai_response(response_text, all_tags):
    """Parse and validate OpenAI JSON response."""
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return None

    technology = data.get("technology", "").strip()
    tags = data.get("tags", [])
    short_description = data.get("short_description", "").strip()
    confidence = data.get("confidence", 0)

    if not isinstance(tags, list):
        return None

    valid_tags = [t for t in tags if t in all_tags]
    if not valid_tags:
        return None

    return {
        "technology": technology,
        "tags": valid_tags,
        "short_description": short_description,
        "confidence": confidence,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Classify unresolved npm libraries using OpenAI API."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of libraries to classify.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test without saving to database.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only classify rows with no existing classification_source.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("Loading tags taxonomy...")
    all_tags, tag_groups = load_tags_taxonomy(TAGS_TAXONOMY_FILE)
    print(f"Loaded {len(all_tags)} allowed tags from taxonomy.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    ensure_columns(cursor)
    conn.commit()

    print("Querying unclassified npm libraries with descriptions...")
    rows = query_unclassified(cursor, args)

    if not rows:
        print("No unclassified libraries found.")
        return

    total = len(rows)
    print(f"Found {total} libraries to classify.")
    if args.dry_run:
        print("DRY-RUN mode: no changes will be saved.")

    stats = {"processed": 0, "success": 0, "invalid": 0, "error": 0}

    for index, record in enumerate(rows, start=1):
        library_id = record[0]
        name = record[1]

        try:
            prompt = build_openai_prompt(record, all_tags, tag_groups)
            response = call_openai_api(prompt)
            result = parse_openai_response(response, all_tags)

            if result is None:
                print(f"  {index}. {name}: INVALID response")
                stats["invalid"] += 1
            else:
                tags_json = json.dumps(result["tags"])
                timestamp = datetime.datetime.utcnow().isoformat()

                if not args.dry_run:
                    cursor.execute(
                        "UPDATE library_catalog SET technology = ?, tags = ?, short_description = ?, classification_source = ?, confidence = ?, ai_model = ?, ai_classified_at = ? WHERE library_id = ?",
                        (
                            result["technology"],
                            tags_json,
                            result["short_description"],
                            "openai",
                            result["confidence"],
                            "gpt-3.5-turbo",
                            timestamp,
                            library_id,
                        ),
                    )
                    conn.commit()

                print(
                    f"  {index}. {name}: {result['technology']} | confidence={result['confidence']:.2f}"
                )
                stats["success"] += 1

        except Exception as exc:
            print(f"  {index}. {name}: ERROR - {exc}")
            stats["error"] += 1

        stats["processed"] += 1
        if index % 5 == 0 or index == total:
            print(f"Processed {index}/{total}...")

    print("--- Classification complete ---")
    print(f"Total: {total}")
    print(f"Processed: {stats['processed']}")
    print(f"Success: {stats['success']}")
    print(f"Invalid: {stats['invalid']}")
    print(f"Error: {stats['error']}")
    if args.dry_run:
        print("DRY-RUN: no changes saved to database.")

    conn.close()


if __name__ == "__main__":
    main()
