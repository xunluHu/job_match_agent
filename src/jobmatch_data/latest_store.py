"""SQLite store and saved profile for the latest-jobs feature. Separate from data/jobs.sqlite3."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

JOB_TYPES = {"intern": {"intern"}, "fulltime": {"newgrad"}, "both": {"intern", "newgrad"}}
LISTING_FIELDS = ("platform", "url", "api_url", "jid", "company", "title", "locations", "posted", "kind",
                  "category", "sponsorship")
POOL_FIELDS = ("key", "company", "title", "url", "locations", "posted", "kind", "description", "degree",
               "min_years", "visa")
PRUNE_DAYS = 30


def connect(db):
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, platform TEXT, url TEXT, api_url TEXT, jid TEXT,
            company TEXT, title TEXT, locations TEXT, posted INTEGER, kind TEXT, category TEXT, sponsorship TEXT,
            active INTEGER NOT NULL DEFAULT 1, jd_status TEXT NOT NULL, description TEXT,
            degree TEXT, min_years INTEGER, visa TEXT, first_seen REAL, last_seen REAL);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
    return conn


def _session(db):
    return closing(connect(db))


def _values(row):
    return [json.dumps(row["locations"]) if name == "locations" else row[name] for name in LISTING_FIELDS]


def sync_listings(db, rows, now):
    report = {"new": 0, "seen": 0, "deactivated": 0, "pruned": 0}
    with _session(db) as conn, conn:
        existing = {r[0] for r in conn.execute("SELECT key FROM jobs")}
        assign = ", ".join(f"{name} = ?" for name in LISTING_FIELDS)
        for row in rows:
            if row["key"] in existing:
                # A row unsupported before (for example Workday before it was added) becomes fetchable.
                conn.execute(f"""UPDATE jobs SET {assign}, active = 1, last_seen = ?,
                                 jd_status = CASE WHEN jd_status = 'unsupported' AND ? IS NOT NULL
                                             THEN 'pending' ELSE jd_status END WHERE key = ?""",
                             [*_values(row), now, row["api_url"], row["key"]])
                report["seen"] += 1
            else:
                status = "pending" if row["api_url"] else "unsupported"
                conn.execute(f"""INSERT INTO jobs (key, {", ".join(LISTING_FIELDS)}, active, jd_status,
                                 first_seen, last_seen) VALUES (?, {", ".join("?" * len(LISTING_FIELDS))}, 1, ?, ?, ?)""",
                             [row["key"], *_values(row), status, now, now])
                existing.add(row["key"])
                report["new"] += 1
        keys = {row["key"] for row in rows}
        missing = [k for (k,) in conn.execute("SELECT key FROM jobs WHERE active = 1") if k not in keys]
        conn.executemany("UPDATE jobs SET active = 0 WHERE key = ?", [(k,) for k in missing])
        report["deactivated"] = len(missing)
        report["pruned"] = conn.execute("DELETE FROM jobs WHERE posted < ?", (now - PRUNE_DAYS * 86400,)).rowcount
    return report


def pending_jd(db):
    with _session(db) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT key, title, platform, api_url, jid, sponsorship FROM jobs
               WHERE active = 1 AND jd_status = 'pending' ORDER BY posted DESC""")]


def save_jd(db, key, description, fields):
    with _session(db) as conn, conn:
        if description is None:
            conn.execute("UPDATE jobs SET jd_status = 'unavailable' WHERE key = ?", (key,))
        else:
            conn.execute("""UPDATE jobs SET jd_status = 'ok', description = ?, degree = ?, min_years = ?, visa = ?
                            WHERE key = ?""",
                         (description, json.dumps(fields["degree"]), fields["min_years"], fields["visa"], key))


def pool(db, kinds, days, now, needs_sponsorship):
    kinds = sorted(kinds)
    sql = f"""SELECT {", ".join(POOL_FIELDS)} FROM jobs WHERE active = 1 AND jd_status = 'ok'
              AND kind IN ({", ".join("?" * len(kinds))}) AND posted >= ?"""
    if needs_sponsorship:
        sql += " AND coalesce(visa, '') NOT IN ('no_sponsorship', 'citizenship_required')"
    with _session(db) as conn:
        rows = conn.execute(sql + " ORDER BY posted DESC, key", [*kinds, now - days * 86400]).fetchall()
    jobs, seen = [], set()
    for row in rows:
        ident = (row["company"].casefold(), row["title"].casefold())
        if ident in seen:
            continue
        seen.add(ident)
        job = dict(row)
        job["locations"] = json.loads(job["locations"] or "[]")
        job["degree"] = json.loads(job["degree"] or "[]")
        jobs.append(job)
    return jobs


def counts(db, days, now):
    with _session(db) as conn:
        row = conn.execute("""SELECT count(*), coalesce(sum(jd_status = 'ok'), 0), coalesce(sum(jd_status = 'pending'), 0),
                              coalesce(sum(jd_status IN ('unsupported', 'unavailable')), 0)
                              FROM jobs WHERE active = 1 AND posted >= ?""", (now - days * 86400,)).fetchone()
    return dict(zip(("total", "with_jd", "pending", "no_jd"), tuple(row)))


def get_meta(db, k):
    with _session(db) as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else None


def set_meta(db, k, v):
    with _session(db) as conn, conn:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def load_profile(data_dir):
    folder = Path(data_dir) / "profile"
    try:
        resume = (folder / "resume.md").read_text(encoding="utf-8")
        settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"resume": resume, "job_type": settings.get("job_type", "both"),
            "needs_sponsorship": bool(settings.get("needs_sponsorship", False)),
            "updated_at": settings.get("updated_at")}


def save_profile(data_dir, resume, job_type, needs_sponsorship, now):
    if job_type not in JOB_TYPES:
        raise ValueError("Job type must be intern, fulltime or both.")
    if not isinstance(needs_sponsorship, bool):
        raise ValueError("The visa sponsorship setting must be true or false.")
    if not isinstance(resume, str) or not resume.strip() or len(resume) > 80_000:
        raise ValueError("Resume text must be between 1 and 80,000 characters.")
    folder = Path(data_dir) / "profile"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "resume.md").write_text(resume, encoding="utf-8")
    settings = {"job_type": job_type, "needs_sponsorship": needs_sponsorship, "updated_at": now}
    (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return {"resume": resume, **settings}
