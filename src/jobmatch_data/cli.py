"""Import normalized files or collect public job postings."""

import argparse
import json
import sqlite3
from pathlib import Path

from .pipeline import collect, export_data, summarize
from .storage import ingest
from .resume_match import DEFAULT_DB, run_cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    match = commands.add_parser("match", help="Match a Markdown/text resume using OpenAI or Gemini")
    match.add_argument("resume", type=Path)
    match.add_argument("--database", type=Path, default=DEFAULT_DB)
    match.add_argument("--output", type=Path, default=Path("data/matches/openai/latest"))
    match.add_argument("--provider", choices=["openai", "gemini"], default="openai")
    match.add_argument("--max-output-tokens", type=int, default=4000)
    match.add_argument("--key-file", type=Path)
    match.add_argument("--model")
    match.add_argument("--top-k", type=int, default=5)
    match.add_argument("--shortlist", type=int, default=7)
    match.add_argument("--direction", choices=["jobs", "candidates"], default="jobs",
                       help="jobs: resume to jobs; candidates: job description to resumes")
    serve = commands.add_parser("serve", help="Open a local resume-matching web app")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--key-file", type=Path)
    serve.add_argument("--model")
    serve.add_argument("--provider", choices=["openai", "gemini"], default="openai")
    serve.add_argument("--max-output-tokens", type=int, default=4000)
    serve.add_argument("--database", type=Path, default=DEFAULT_DB)
    fetch = commands.add_parser("collect", help="Download Simplify history and discovered ATS boards")
    fetch.add_argument("--repos", nargs="+", default=["New-Grad-Positions", "Summer2027-Internships"])
    fetch.add_argument("--ref", default="dev", help="Git branch/tag/commit SHA for Simplify JSON")
    fetch.add_argument("--boards-per-vendor", type=int, default=2)
    fetch.add_argument("--boards-file", type=Path)
    fetch.add_argument("--data-dir", type=Path, default=Path("data"))
    local = commands.add_parser("import", help="Import normalized JSON")
    local.add_argument("file", type=Path)
    local.add_argument("--source", required=True)
    local.add_argument("--data-dir", type=Path, default=Path("data"))
    for command in ("stats", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    try:
        if args.command == "match":
            run_cli(args)
        elif args.command == "serve":
            from .webapp import serve
            serve(args)
        elif args.command == "collect":
            if args.boards_per_vendor < 0:
                parser.error("--boards-per-vendor must be nonnegative")
            report = collect(args.data_dir, args.repos, args.ref, args.boards_per_vendor, args.boards_file)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["errors"]:
                parser.exit(1, "Collection partially failed; see run manifest.\n")
        elif args.command == "import":
            print(f"Imported {ingest(args.file, args.source, args.data_dir)} records")
        elif args.command == "stats":
            print(json.dumps(summarize(args.data_dir), ensure_ascii=False, indent=2))
        else:
            export_data(args.data_dir)
            print(f"Exported to {args.data_dir / 'exports'}")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"Operation failed: {exc}\n")


if __name__ == "__main__":
    main()
