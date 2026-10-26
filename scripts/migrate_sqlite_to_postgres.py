"""One-time copy of the escalation SQLite DB into the private PostgreSQL schema.

Creates the tables (same DDL the service uses), copies every row and resets id sequences.
Idempotent: existing rows are skipped (ON CONFLICT DO NOTHING), so it can be re-run.
Stop the service first so SQLite is not written during the copy.

Usage: python scripts/migrate_sqlite_to_postgres.py --database-url postgresql://user:pass@host:5432/db
"""
from __future__ import annotations
import argparse
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from app.escalation_analyst.repositories.database import Database
from app.escalation_analyst.settings import Settings

BATCH = 1000


def migrate(sqlite_path: str, database_url: str) -> None:
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("A PostgreSQL database URL is required")
    path = Path(sqlite_path).resolve(strict=True)
    db = Database(replace(Settings(), database_url=database_url))
    db.initialize()
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    # Hold a consistent read snapshot even if a writer is still running.
    source.execute("BEGIN")
    tables = [r[0] for r in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    with db.connection() as conn:
        for table in tables:
            target = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='escalation_analyst' "
                "AND table_name=%s", (table,))}
            if not target:
                raise ValueError(f"Source table {table} is missing from PostgreSQL; copy rolled back")
            missing = {r[1] for r in source.execute(f'PRAGMA table_info("{table}")')} - target
            if missing:
                raise ValueError(f"Missing PostgreSQL columns in {table}: {sorted(missing)}")
            columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")') if r[1] in target]
            names = ",".join(f'"{c}"' for c in columns)
            insert = (f'INSERT INTO "{table}" ({names}) VALUES ({",".join(["%s"] * len(columns))}) '
                      "ON CONFLICT DO NOTHING")
            rows = source.execute(f'SELECT {names} FROM "{table}"')
            copied = 0
            inserted = 0
            with conn.cursor() as cur:
                while chunk := rows.fetchmany(BATCH):
                    cur.executemany(insert, chunk)
                    copied += len(chunk)
                    inserted += cur.rowcount
            keys = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")') if r[5]]
            if not keys:
                raise ValueError(f"Cannot verify {table}: no primary key")
            key_names = ",".join(f'"{c}"' for c in keys)
            target_keys = {tuple(row[c] for c in keys) for row in conn.execute(f'SELECT {key_names} FROM "{table}"')}
            if any(tuple(row) not in target_keys for row in source.execute(f'SELECT {key_names} FROM "{table}"')):
                raise ValueError(f"Some source keys are missing in {table}; copy rolled back")
            if "id" in target and conn.execute(
                    "SELECT pg_get_serial_sequence(%s,'id') AS seq", (f"escalation_analyst.{table}",)).fetchone()["seq"]:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('escalation_analyst.{table}','id'), "
                             f'COALESCE((SELECT MAX(id) FROM "{table}"), 0) + 1, false)')
            total = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"]
            print(f"{table}: read {copied}, inserted {inserted}, now {total} in PostgreSQL; source keys verified")
    source.close()
    print("Migration committed successfully. Original SQLite file preserved.")


def main():
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", default=settings.sqlite_path)
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or ESCALATION_DATABASE_URL is required")
    migrate(args.sqlite, args.database_url)


if __name__ == "__main__":
    main()
