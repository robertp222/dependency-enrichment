import re
import sqlite3
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

BASE_DIR = Path(__file__).resolve().parent.parent
RULES_FILE = BASE_DIR / "rules" / "technology_rules.yaml"
DB_FILE = BASE_DIR / "db" / "libraries.db"


def parse_scalar(value):
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",")]

    if text.isdigit():
        return int(text)

    lower = text.lower()
    if lower in {"true", "false"}:
        return lower == "true"

    return text.strip('"').strip("'")


def parse_simple_yaml(text):
    items = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        if line.startswith("- "):
            if current is not None:
                items.append(current)
            current = {}
            line = line[2:]
        elif current is None:
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        current[key.strip()] = parse_scalar(value.strip())

    if current is not None:
        items.append(current)
    return items


def load_rules(path):
    with open(path, "r", encoding="utf-8") as handle:
        contents = handle.read()

    if yaml is not None:
        rules = yaml.safe_load(contents)
    else:
        rules = parse_simple_yaml(contents)

    if not isinstance(rules, list):
        raise ValueError("Rules YAML must be a list of rule definitions.")

    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue

        pattern = rule.get("pattern")
        if not pattern:
            raise ValueError(f"Rule is missing pattern: {rule}")

        type_filter = rule.get("type_filter")
        if isinstance(type_filter, str):
            type_filter = [type_filter]

        normalized.append(
            {
                "name": rule.get("name", "unnamed_rule"),
                "pattern": re.compile(pattern),
                "type_filter": [t.strip() for t in type_filter] if type_filter else None,
                "technology": rule.get("technology", ""),
                "category": rule.get("category", ""),
                "confidence": rule.get("confidence", None),
            }
        )
    return normalized


def ensure_columns(cursor):
    cursor.execute("PRAGMA table_info(library_catalog)")
    columns = {row[1] for row in cursor.fetchall()}

    if "classification_source" not in columns:
        cursor.execute(
            "ALTER TABLE library_catalog ADD COLUMN classification_source TEXT DEFAULT ''"
        )

    if "confidence" not in columns:
        cursor.execute(
            "ALTER TABLE library_catalog ADD COLUMN confidence INTEGER"
        )


def classify_library(row, rules):
    library_id, type_value, normalized_name = row

    for rule in rules:
        if rule["type_filter"] and type_value not in rule["type_filter"]:
            continue

        if rule["pattern"].search(normalized_name):
            return {
                "technology": rule["technology"],
                "category": rule["category"],
                "classification_source": rule["name"],
                "confidence": rule["confidence"],
            }

    return None


def main():
    print(f"Loading rules: {RULES_FILE}")
    rules = load_rules(RULES_FILE)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    ensure_columns(cursor)
    conn.commit()

    cursor.execute(
        "SELECT library_id, type, normalized_name FROM library_catalog"
    )
    rows = cursor.fetchall()

    if not rows:
        print("No libraries found in library_catalog.")
        return

    updated = 0
    for row in rows:
        result = classify_library(row, rules)
        if not result:
            continue

        cursor.execute(
            "UPDATE library_catalog SET technology = ?, category = ?, classification_source = ?, confidence = ? WHERE library_id = ?",
            (
                result["technology"],
                result["category"],
                result["classification_source"],
                result["confidence"],
                row[0],
            ),
        )
        updated += 1

    conn.commit()

    cursor.execute(
        "SELECT COUNT(*) FROM library_catalog"
    )
    total = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM library_catalog WHERE classification_source IS NOT NULL AND classification_source != ''"
    )
    classified = cursor.fetchone()[0]
    unclassified = total - classified

    print("--- Classification summary ---")
    print(f"Total libraries: {total}")
    print(f"Classified: {classified}")
    print(f"Unclassified: {unclassified}")
    print(f"Updated rows: {updated}")

    conn.close()


if __name__ == "__main__":
    main()
