"""Bounded collection with independently committed sources and run manifests."""
import csv
import json
from collections import Counter
from datetime import datetime, timezone

from .http import Client
from .sources import ats, discover_boards, simplify
from .storage import connect, save


def summarize(root):
    conn = connect(root)
    try:
        totals = conn.execute("""SELECT count(*), count(DISTINCT company),
            sum(active=1), sum(active=0), sum(description IS NOT NULL AND description != ''),
            min(posted_at), max(posted_at) FROM jobs""").fetchone()
        sources = dict(conn.execute("SELECT source, count(*) FROM jobs GROUP BY source"))
        return dict(zip(("records", "companies", "active", "inactive", "with_description",
                         "earliest_source_posted_at", "latest_source_posted_at"), totals),
                    sources=sources,
                    versions=conn.execute("SELECT count(*) FROM job_versions").fetchone()[0])
    finally:
        conn.close()


def export_data(root):
    folder = root / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    conn = connect(root)
    try:
        cursor = conn.execute("""SELECT source,source_id,title,company,location,url,posted_at,
            active,source_updated_at,first_seen_at,last_seen_at,description FROM jobs
            ORDER BY source,source_id""")
        fields = [col[0] for col in cursor.description]
        with (folder / "jobs.csv").open("w", encoding="utf-8-sig", newline="") as csv_file, \
                (folder / "jobs.jsonl").open("w", encoding="utf-8") as json_file:
            writer = csv.writer(csv_file)
            writer.writerow(fields)
            for row in cursor:
                writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in row])
                json_file.write(json.dumps(dict(zip(fields, row)), ensure_ascii=False) + "\n")
    finally:
        conn.close()


def collect(root, repos, ref, boards_per_vendor, boards_file=None):
    client = Client(root)
    started_at = datetime.now(timezone.utc).isoformat()
    report = {"started_at": started_at, "completed": [], "errors": [], "responses": client.responses}
    discovered = []

    def run(source, loader):
        try:
            rows = loader()
            observed = client.responses[-1]["fetched_at"] if client.responses else started_at
            count = save(rows, source, root, observed)
            report["completed"].append({"source": source, "records": count})
            print(f"{source}: {count} records", flush=True)
            return rows
        except Exception as exc:
            report["errors"].append({"source": source, "error": str(exc)})
            print(f"{source}: FAILED: {exc}", flush=True)
            return []

    for repo in repos:
        discovered.extend(run(f"simplify:{repo}", lambda repo=repo: simplify(client, repo, ref)))
    boards = discover_boards(discovered)
    root.mkdir(parents=True, exist_ok=True)
    (root / "discovered_boards.json").write_text(json.dumps(boards, ensure_ascii=False, indent=2), encoding="utf-8")
    if boards_file:
        selected = json.loads(boards_file.read_text(encoding="utf-8"))
        if not isinstance(selected, list):
            raise ValueError("Boards file must be an array")
    else:
        counts = Counter()
        selected = []
        for board in boards:
            vendor = board["vendor"]
            if counts[vendor] < boards_per_vendor:
                selected.append(board)
                counts[vendor] += 1
    for board in selected:
        vendor, token = board["vendor"], board["board"]
        region = board.get("region", "global")
        run(f"{vendor}:{region}:{token}", lambda board=board: ats(client, **board))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["stats"] = summarize(root)
    export_data(root)
    folder = root / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    name = started_at.replace(":", "-") + ".json"
    (folder / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(report["stats"], ensure_ascii=False, indent=2), encoding="utf-8")
    return report
