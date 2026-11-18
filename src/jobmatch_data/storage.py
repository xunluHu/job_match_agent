"""SQLite latest records plus distinct content versions."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone


def connect(root):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "jobs.sqlite3")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        source TEXT NOT NULL, source_id TEXT NOT NULL,
        title TEXT NOT NULL, company TEXT NOT NULL, location TEXT,
        url TEXT NOT NULL, posted_at TEXT, description TEXT,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        raw_json TEXT NOT NULL, PRIMARY KEY (source, source_id))""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, kind in (("active", "INTEGER"), ("source_updated_at", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
    conn.execute("""CREATE TABLE IF NOT EXISTS job_versions (
        source TEXT NOT NULL, source_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        observed_at TEXT NOT NULL, raw_json TEXT NOT NULL,
        PRIMARY KEY(source, source_id, content_sha256))""")
    conn.commit()
    return conn


def save(records, source, root, observed_at=None):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Source must be nonempty")
    if not isinstance(records, list):
        raise ValueError("Input must be a JSON array")
    now = observed_at or datetime.now(timezone.utc).isoformat()
    rows, versions = [], []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index}: expected an object")
        for field in ("id", "title", "company", "url"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"Record {index}: missing nonempty string {field}")
        for field in ("posted_at", "source_updated_at"):
            if record.get(field) is not None:
                if not isinstance(record[field], str):
                    raise ValueError(f"Record {index}: {field} must be an ISO date string")
                datetime.fromisoformat(record[field].replace("Z", "+00:00"))
        for field in ("location", "description"):
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f"Record {index}: {field} must be string or null")
        if record.get("active") is not None and type(record["active"]) is not bool:
            raise ValueError(f"Record {index}: active must be boolean or null")
        raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
        rows.append((source, record["id"], record["title"], record["company"],
                     record.get("location"), record["url"], record.get("posted_at"),
                     record.get("description"), now, now, raw_json,
                     record.get("active"), record.get("source_updated_at")))
        versions.append((source, record["id"], hashlib.sha256(raw_json.encode()).hexdigest(), now, raw_json))
    conn = connect(root)
    try:
        with conn:
            conn.executemany("""INSERT INTO jobs
                (source,source_id,title,company,location,url,posted_at,description,
                 first_seen_at,last_seen_at,raw_json,active,source_updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                location=excluded.location, url=excluded.url,
                posted_at=excluded.posted_at, description=excluded.description,
                last_seen_at=excluded.last_seen_at, raw_json=excluded.raw_json,
                active=excluded.active, source_updated_at=excluded.source_updated_at""", rows)
            conn.executemany("INSERT OR IGNORE INTO job_versions VALUES (?,?,?,?,?)", versions)
    finally:
        conn.close()
    return len(rows)


def ingest(path, source, root):
    raw = path.read_bytes()
    count = save(json.loads(raw), source, root)
    archive = root / "raw"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{hashlib.sha256(raw).hexdigest()}.json").write_bytes(raw)
    return count
