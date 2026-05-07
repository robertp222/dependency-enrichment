import sqlite3
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent.parent
CSV_FILE = BASE_DIR / "data" / "dependencies.csv"
DB_FILE = BASE_DIR / "db" / "libraries.db"


def normalize_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize_name(value):
    return normalize_text(value).lower()


def main():
    print(f"Loading CSV: {CSV_FILE}")

    df = pd.read_csv(CSV_FILE)

    required_columns = {"id", "type", "name", "technology", "category"}
    missing = required_columns - set(df.columns)

    if missing:
        raise ValueError(f"Missing columns in CSV: {missing}")

    df["type"] = df["type"].apply(normalize_text)
    df["name"] = df["name"].apply(normalize_text)
    df["normalized_name"] = df["name"].apply(normalize_name)
    df["technology"] = df["technology"].apply(normalize_text)
    df["category"] = df["category"].apply(normalize_text)

    catalog_df = (
        df[["type", "name", "normalized_name", "technology", "category"]]
        .drop_duplicates(subset=["type", "normalized_name"])
        .sort_values(by=["type", "normalized_name"])
        .reset_index(drop=True)
    )

    catalog_df.insert(0, "library_id", range(1, len(catalog_df) + 1))

    print(f"Input rows: {len(df)}")
    print(f"Unique libraries: {len(catalog_df)}")

    conn = sqlite3.connect(DB_FILE)

    df.to_sql("dependencies_raw", conn, if_exists="replace", index=False)
    catalog_df.to_sql("library_catalog", conn, if_exists="replace", index=False)

    conn.close()

    print(f"Database created: {DB_FILE}")
    print("DONE")


if __name__ == "__main__":
    main()