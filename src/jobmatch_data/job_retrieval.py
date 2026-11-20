"""Section-level BM25 over the current SQLite job archive."""
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from threading import Lock

from .job_qualification import candidate_profile, job_profile, qualification, screening_order
from .matching import ALIASES, STOPWORDS


HEADINGS = re.compile(
    r"(?P<heading>responsibilities|(?:key|core|job|primary) responsibilities|"
    r"what you(?:'|’)ll do|what you will do|your impact|"
    r"(?:(?:basic|minimum|required|preferred|desired|additional) )?qualifications|"
    r"(?:(?:minimum|required|preferred|job) )?requirements|what you(?:'|’)ll need|"
    r"what you bring|who you are|must[- ]haves?|nice[- ]to[- ]haves?|"
    r"bonus points|about (?:the role|us)|benefits|"
    r"岗位职责|工作职责|任职要求|基本要求|优先条件|加分项|福利)"
    r"(?:\s*[:：]\s*(?P<body>.*)|\s*)", re.I
)
WORDS = re.compile(r"[a-z][a-z0-9+#_]*(?:[.-][a-z0-9+#_]+)*|[\u3400-\u9fff]+")


def terms(text):
    for word in WORDS.findall(unicodedata.normalize("NFKC", text).lower()):
        if "\u3400" <= word[0] <= "\u9fff":
            if len(word) == 1:
                yield word
            else:
                yield from (word[i:i+2] for i in range(len(word)-1))
        elif word not in STOPWORDS and len(word) > 1:
            yield word


def fts_term(word):
    # Encode punctuation and CJK so SQLite sees the same tokens as the query parser.
    return word if word.isascii() and word.isalnum() and not word.startswith("zz") else "zz" + word.encode().hex()


def sections(description, limit=1200):
    """Split at recognized headings, using paragraphs to bound passage length."""
    label = "Job description"
    lines = []
    for line in description.splitlines():
        heading = HEADINGS.fullmatch(line.strip().lstrip("# ").replace("**", "").strip())
        if heading:
            yield from passages(label, lines, limit)
            label, lines = heading["heading"], []
            # Match against the original text for inline evidence.
            line = re.split(r"[:：]", line, maxsplit=1)[-1] if heading["body"] else ""
        if line.strip():
            lines.append(line.strip())
    yield from passages(label, lines, limit)


def passages(label, lines, limit):
    passage = []
    size = 0
    for line in lines:
        while len(line) > limit:
            split = line.rfind(" ", 0, limit + 1)
            split = split if split > 0 else limit
            if passage:
                yield label, "\n".join(passage)
                passage, size = [], 0
            yield label, line[:split]
            line = line[split:].lstrip()
        if not line:
            continue
        if size + len(line) > limit and passage:
            yield label, "\n".join(passage)
            passage, size = [], 0
        passage.append(line)
        size += len(line) + 1
    if passage:
        yield label, "\n".join(passage)


TECHNICAL_TERMS = {word for aliases in ALIASES.values() for alias in aliases
                   if len(words := list(terms(alias))) == 1 for word in words}
TECHNICAL_TERMS.update(("cuda", "fpga", "frontend", "backend", "zustand", "webpack", "css3", "html5"))


def duplicate_key(job):
    return tuple(" ".join((job.get(field) or "").casefold().split())
                 for field in ("company", "title", "location"))


class SectionSearch:
    def __init__(self, jobs):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = Lock()
        self.df = Counter()
        self.passage_count = 0
        self.profiles = []
        self.company_counts = Counter(job["company"] for job in jobs)
        try:
            self.conn.execute("""CREATE VIRTUAL TABLE passages USING fts5(
                body, job_index UNINDEXED, section UNINDEXED, text UNINDEXED)""")
            with self.conn:
                self.conn.executemany("INSERT INTO passages VALUES (?, ?, ?, ?)", self.rows(jobs))
        except sqlite3.Error as exc:
            self.conn.close()
            raise RuntimeError(f"Could not build the SQLite FTS5 job index: {exc}") from exc

    def rows(self, jobs):
        for i, job in enumerate(jobs):
            passages = list(sections(job["description"]))
            self.profiles.append(job_profile(job["title"], passages))
            for section, body in passages:
                words = list(terms(body))
                if words:
                    self.df.update(set(words))
                    self.passage_count += 1
                    yield " ".join(map(fts_term, words)), i, section, body

    def retrieve(self, resume, jobs, limit, query=None):
        """Search with the resume's own terms, or with `query` (an agent's search words); screening uses the resume."""
        profile, focus = candidate_profile(resume)
        focus_terms = set(terms(focus))
        searched = query

        def query_weight(word, count):
            priority = 2 if word in focus_terms else 1
            return priority * math.log1p(min(count, 3)) * math.log1p(self.passage_count / self.df[word])

        weights = {word: query_weight(word, count) for word, count in Counter(terms(searched or resume)).items()
                   if self.df[word]}
        technical = [word for word in weights if word in TECHNICAL_TERMS]
        focused = [word for word in technical if word in focus_terms]
        # Search words are already chosen; only a whole resume needs narrowing to its technical terms.
        pool = weights if searched else focused if len(focused) >= 4 else technical if len(technical) >= 3 else weights
        query = sorted(pool, key=lambda word: (-weights[word], word))[:12]
        expression = " OR ".join(fts_term(word) for word in query)
        recalled, matched_jobs = {}, set()
        matched_passages = 0
        duplicate_jobs = 0
        bm25_companies = Counter()
        if expression:
            with self.lock:
                rows = self.conn.execute("""SELECT rowid, job_index, bm25(passages)
                    FROM passages WHERE passages MATCH ? ORDER BY 3, rowid""", (expression,))
                hits = []
                for rowid, index, score in rows:
                    matched_passages += 1
                    if index in matched_jobs:
                        continue
                    matched_jobs.add(index)
                    hits.append((index, rowid, score, qualification(profile, self.profiles[index])))
                    if len(hits) <= 50:
                        bm25_companies[jobs[index]["company"]] += 1
                urls, postings = set(), {}
                for index, rowid, score, screen in sorted(hits, key=lambda item: screening_order(item[3])):
                    if len(recalled) >= max(35, limit):
                        break
                    job = jobs[index]
                    key = duplicate_key(job) + (self.profiles[index]["min_years"],)
                    lines = [" ".join(line.casefold().split()) for line in job["description"].splitlines() if line.strip()]
                    variants = postings.setdefault(key, [])
                    if job["url"] in urls or any(SequenceMatcher(None, previous, lines, autojunk=False).ratio() >= 0.9
                                                 for previous in variants):
                        duplicate_jobs += 1
                        continue
                    urls.add(job["url"])
                    variants.append(lines)
                    recalled[index] = (rowid, score, screen)
                results = []
                for index, (rowid, score, screen) in list(recalled.items())[:limit]:
                    section, body = self.conn.execute(
                        "SELECT section, text FROM passages WHERE rowid = ?", (rowid,)).fetchone()
                    matched = set(terms(body))
                    quote = max(body.splitlines(),
                                key=lambda line: sum(weights[w] for w in set(terms(line)) if w in query))
                    results.append(dict(jobs[index], job_ref=f"J{len(results)+1:03d}",
                                        retrieval_score=round(-score, 6),
                                        screening=screen,
                                        retrieval_hit={"section": section, "quote": quote,
                                                       "matched_terms": [w for w in query if w in matched]}))
        else:
            results = []
        return results, {
            "method": "sqlite_fts5_bm25",
            "candidate_profile": profile,
            "query_terms": query,
            "matched_jobs": len(matched_jobs),
            "matched_passages": matched_passages,
            "indexed_passages": self.passage_count,
            "screened_jobs": len(recalled),
            "duplicate_jobs": duplicate_jobs,
            "company_distribution": {
                "archive_leaders": self.company_counts.most_common(3),
                "bm25_top_50": bm25_companies.most_common(),
                "shortlist": Counter(job["company"] for job in results).most_common(),
            },
            "shortlist": [{key: job[key] for key in
                           ("source", "source_id", "title", "company", "location", "retrieval_score", "retrieval_hit", "screening")}
                          for job in results],
        }
