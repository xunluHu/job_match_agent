"""Explainable skill/role retrieval baseline, not a hiring decision model."""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path


ALIASES = {
    "Python": ["python"], "SQL": ["sql"], "PySpark": ["pyspark"],
    "Spark": ["apache spark", "spark"], "SparkR": ["sparkr"],
    "Azure Data Factory": ["azure data factory", "adf"],
    "Google Cloud Storage": ["google cloud storage", "gcs"],
    "data warehousing": ["data warehousing", "data warehouse", "data warehouses"],
    "ETL": ["etl", "extract transform load"],
    "Snowflake": ["snowflake"], "Amazon Redshift": ["redshift"],
    "Airflow": ["airflow"], "Databricks": ["databricks"], "SAP": ["sap"],
    "AWS": ["aws", "amazon web services"], "Amazon S3": ["amazon s3", "aws s3", "s3"],
    "Tableau": ["tableau"], "R": ["r"], "Hive": ["hive"],
    "Node.js": ["node.js", "nodejs", "node js"], "JavaScript": ["javascript"],
    "REST APIs": ["rest api", "rest apis", "restful", "rest-based"],
    "FastAPI": ["fastapi"], "Django": ["django"], "MongoDB": ["mongodb"],
    "PostgreSQL": ["postgresql", "postgres"], "Docker": ["docker"],
    "RAG": ["rag", "retrieval augmented generation", "retrieval-augmented generation"],
    "Java": ["java"], "Spring Boot": ["spring boot", "springboot"],
    "TypeScript": ["typescript"], "Next.js": ["next.js", "nextjs"],
    "SQL Server": ["sql server", "mssql"], "Express.js": ["express.js", "expressjs"],
    "relational databases": ["relational database", "relational databases", "rdbms"],
    "React": ["react", "react.js", "reactjs"], "Redux": ["redux"],
    "web performance": ["web performance", "frontend performance", "front-end performance"],
    "React Native": ["react native"], "MobX": ["mobx"], "GraphQL": ["graphql"],
    "Redis": ["redis"], "SSR": ["ssr", "server-side rendering", "server side rendering"],
    "Kotlin": ["kotlin"], "Swift": ["swift"], "Preact": ["preact"],
    "TanStack Query": ["tanstack query", "react query"],
    "CI/CD": ["ci/cd", "ci / cd", "cicd", "continuous integration", "continuous delivery"],
    "Kubernetes": ["kubernetes", "k8s"], "Amazon EKS": ["eks", "elastic kubernetes service"],
    "Terraform": ["terraform"], "GitLab CI": ["gitlab ci", "gitlab-ci", "gitlab ci/cd"],
    "Karpenter": ["karpenter"], "KEDA": ["keda"], "GitHub Actions": ["github actions"],
    "Azure": ["azure"], "Helm": ["helm"], "Git": ["git"], "Jenkins": ["jenkins"],
    "Ansible": ["ansible"], "Prometheus": ["prometheus"], "Grafana": ["grafana"],
    "Linux": ["linux"], "NGINX": ["nginx"], "Go": ["golang", "go language", "go programming", "go"],
    "C++": ["c++", "c++17", "c++20"], "C#": ["c#"],
    "TensorFlow": ["tensorflow"], "PyTorch": ["pytorch"], "scikit-learn": ["scikit-learn", "sklearn"],
    "Pandas": ["pandas"], "NumPy": ["numpy"], "OpenCV": ["opencv"],
    "machine learning": ["machine learning"], "computer vision": ["computer vision"],
    "LLM": ["llm", "llms", "large language model", "large language models"],
    "LangChain": ["langchain"], "Qdrant": ["qdrant"], "Kafka": ["kafka"],
    "Flask": ["flask"], "MySQL": ["mysql"], "dbt": ["dbt"],
    "Power BI": ["power bi", "powerbi"], "Bash": ["bash"],
    "Google Cloud": ["gcp", "google cloud"], "BigQuery": ["bigquery"],
    "Azure Data Lake": ["azure data lake", "adls"], "Azure Synapse": ["azure synapse", "synapse"],
    "AWS Lambda": ["aws lambda", "lambda"], "AWS Glue": ["aws glue"],
    "MATLAB": ["matlab"], "PLC": ["plc", "programmable logic controller"],
    "ROS": ["ros", "ros2"], "embedded systems": ["embedded systems", "embedded software"],
    "RTOS": ["rtos", "real-time operating system"], "GitHub": ["github"],
    "HTML": ["html", "html5"], "CSS": ["css", "css3"], "Angular": ["angular"],
    "Jest": ["jest"], "Cypress": ["cypress"],
}
ROLE_PATTERNS = {
    "data": r"data engineer|analytics engineer|data analyst|data architect|data platform|business intelligence|etl developer|database engineer|data scientist",
    "backend": r"back[- ]?end|full[- ]?stack|software engineer|software developer|java developer|python developer",
    "frontend": r"front[- ]?end|web developer|ui engineer|full[- ]?stack|software engineer|software developer",
    "devops": r"devops|site reliability|\bsre\b|platform engineer|infrastructure engineer|cloud engineer|cloud architect|systems engineer",
    "ml": r"machine learning|\bml\b|\bai\b|artificial intelligence|data scientist|computer vision|mlops|applied scientist",
    "embedded": r"embedded|robotic|mechatronic|manufacturing|controls? engineer|automation engineer|firmware|hardware|systems engineer",
}

STOPWORDS = set("""a an and are as at be been being by can could for from has have in into is it its may more most must of on or our should that the their these they this to using was we were will with you your years year experience skills education work team teams role ability strong including working company position job requirements required preferred responsibilities excellent knowledge understanding degree bachelor master university college communication benefits salary equal opportunity applicant employer gender race religion age disability veteran status employment location""".split())


def tokens(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return Counter(word for raw in re.findall(r"[a-z][a-z0-9+#_.-]+", text)
                   if len(word := raw.strip(".-")) > 1 and word not in STOPWORDS)


def vector(counts, idf):
    values = {word: (1 + math.log(count)) * idf[word] for word, count in counts.items() if word in idf}
    norm = math.sqrt(sum(value * value for value in values.values()))
    return {word: value / norm for word, value in values.items()} if norm else {}


def cosine(left, right):
    return sum(value * right.get(word, 0) for word, value in left.items())


def skill_span(text, skill):
    lowered = text.lower()
    for alias in ALIASES.get(skill, [skill.lower()]):
        if alias.lower() not in lowered:
            continue
        pattern = r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
        for match in re.finditer(pattern, text, re.I):
            # Disambiguate common single-letter/word false positives.
            if skill == "R" and match.group() != "R":
                continue
            if skill == "Go" and alias == "go" and not re.search(
                    r"(?:python|java|golang|programming|rust|c\+\+|language)",
                    text[max(0, match.start()-60):match.end()+60], re.I):
                continue
            return match.span()
    return None


def evidence(text, span, window=130):
    start, end = span
    return text[max(0, start-window):min(len(text), end+window)].strip()


def restriction_evidence(text):
    patterns = {
        "clearance": r"security clearance|secret clearance|top secret|TS/SCI|clearance required|active clearance",
        "citizenship": r"U\.?S\.? citizen(?:ship)?|United States citizen(?:ship)?",
        "work_authorization": r"work authori[sz]ation|visa sponsorship|authorized to work|eligible to work",
    }
    result = {}
    for label, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            result[label] = evidence(text, match.span())
    return result


def rank_profiles(profiles, jobs, top_k=5):
    vocab = set(ALIASES)
    extracted = []
    frequency = Counter()
    for job in jobs:
        text = job["description"]
        spans = {skill: span for skill in vocab if (span := skill_span(text, skill))}
        extracted.append(spans)
        frequency.update(spans.keys())
    weights = {skill: math.log(1 + (len(jobs)+1)/(frequency[skill]+1)) for skill in vocab}
    job_counts = [tokens(job["description"]) for job in jobs]
    df = Counter()
    for counts in job_counts:
        df.update(counts.keys())
    idf = {word: math.log((len(jobs)+1)/(count+1)) + 1 for word, count in df.items() if count >= 2}
    job_vectors = [vector(counts, idf) for counts in job_counts]
    results = []
    for profile in profiles:
        candidate_skills = set(profile["skills"])
        experience_vector = vector(tokens(profile.get("experience_text", "")), idf)
        projects_vector = vector(tokens(profile.get("projects_text", "")), idf)
        matches = []
        for job, spans, job_vector in zip(jobs, extracted, job_vectors):
            if not re.search(ROLE_PATTERNS[profile["role_family"]], job["title"], re.I):
                continue
            overlap = sorted(candidate_skills & spans.keys())
            if len(overlap) < 2:
                continue
            # Technical retrieval only; no names, education, location or years enter scoring.
            recall = sum(weights[s] for s in overlap) / sum(weights.get(s, 1) for s in candidate_skills)
            precision = sum(weights[s] for s in overlap) / sum(weights[s] for s in spans)
            skill_score = 0.75 * recall + 0.25 * precision
            experience_similarity = cosine(experience_vector, job_vector)
            project_similarity = cosine(projects_vector, job_vector)
            full_resume = bool(profile.get("experience_text") and profile.get("projects_text"))
            score = 100 * (0.5 * skill_score + 0.25 * experience_similarity + 0.25 * project_similarity) if full_resume else 100 * skill_score
            resume_evidence = {}
            for skill in overlap:
                for page in profile.get("resume_page_texts", []):
                    span = skill_span(page["text"], skill)
                    if span:
                        resume_evidence[skill] = {"page": page["page"], "text": evidence(page["text"], span, 85)}
                        break
            matches.append({
                "source": job["source"], "source_id": job["source_id"],
                "title": job["title"], "company": job["company"], "location": job["location"],
                "url": job["url"], "retrieval_score": round(score, 2),
                "score_components": {"weighted_skill_score": round(100*skill_score, 2),
                                     "experience_tfidf_cosine": round(experience_similarity, 4),
                                     "projects_tfidf_cosine": round(project_similarity, 4)},
                "matched_skill_mentions": overlap,
                "job_skill_mentions_not_evidenced_in_profile": sorted(spans.keys() - candidate_skills),
                "job_evidence": {s: evidence(job["description"], spans[s]) for s in overlap},
                "resume_evidence": resume_evidence,
                "eligibility": "not_assessed",
                "restriction_mentions_to_verify": restriction_evidence(job["description"]),
                "needs_verification": ["current opening status", "required experience", "work authorization / visa",
                                       "location / relocation", "degree requirements", "security clearance / citizenship",
                                       "whether mentioned skills are mandatory or optional"],
            })
        matches.sort(key=lambda x: (-x["retrieval_score"], x["source"], x["source_id"]))
        # Keep one appearance per application URL within each candidate's shortlist.
        seen, unique = set(), []
        for match in matches:
            if match["url"] not in seen:
                seen.add(match["url"])
                unique.append(match)
        results.append({"candidate_id": profile["candidate_id"], "role_family": profile["role_family"],
                        "profile_headline": profile["headline"], "ranked_jobs": unique[:top_k]})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, default=Path("data/candidates/full_resumes_v1/profiles.json"))
    parser.add_argument("--database", type=Path, default=Path("data/with_description/jobs.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("data/matches/full_resumes_v1"))
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
    if len({p["candidate_id"] for p in profiles}) != len(profiles):
        raise ValueError("Candidate IDs must be unique")
    for p in profiles:
        if not p["skills"] or p["role_family"] not in ROLE_PATTERNS:
            raise ValueError(f"Invalid profile: {p['candidate_id']}")
    conn = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        jobs = [dict(row) for row in conn.execute("""SELECT * FROM jobs
            WHERE description IS NOT NULL AND length(trim(description))>0""")]
    finally:
        conn.close()
    results = rank_profiles(profiles, jobs, args.top_k)
    args.output.mkdir(parents=True, exist_ok=True)
    is_full = all(p.get("experience_text") and p.get("projects_text") for p in profiles)
    report = {"method": "role_filtered_skills_experience_projects_tfidf_v2" if is_full else "deterministic_role_filtered_idf_weighted_skill_retrieval_v1",
              "database": str(args.database), "database_records": len(jobs),
              "database_sha256": hashlib.sha256(args.database.read_bytes()).hexdigest(),
              "profiles_sha256": hashlib.sha256(args.profiles.read_bytes()).hexdigest(),
              "candidate_count": len(profiles), "top_k": args.top_k,
              "score_meaning": "Retrieval heuristic 0-100, not a probability or hiring eligibility score; not comparable across candidates.",
              "results": results}
    (args.output / "matches.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = [f"# {len(profiles)} 份完整简历的岗位检索结果" if is_full else f"# {len(profiles)} 份公开资料的岗位检索结果", "",
                f"数据库：{len(jobs):,} 条有 description 的来源记录；每份资料返回最多 {args.top_k} 个岗位。", "",
                "输入为实际下载的完整 PDF 简历，使用技能、经历和项目正文检索；公开简历的经历未独立认证。" if is_full else "输入为不完整资料摘要。", "",
                "检索分不是录用概率；未判断签证、国籍、学历、年限、地点或岗位是否仍开放。", "",
                "| 资料 | 方向 | 首选检索结果 | 公司 | 命中技能 |", "| --- | --- | --- | --- | --- |"]
    for result in results:
        cid = result["candidate_id"]
        profile = next(p for p in profiles if p["candidate_id"] == cid)
        profile_link = Path(os.path.relpath(profile["resume_pdf"], args.output)).as_posix() if profile.get("resume_pdf") else f"../../candidates/linkedin_15/profiles/{cid}.md"
        page = [f"# {cid} — {result['profile_headline']}", "",
                f"[查看完整简历 PDF]({profile_link})" if is_full else f"[查看公开资料]({profile_link})", "",
                "技能依据见候选资料目录的 provenance.json；以下仅为技术检索，未确认资格或实时岗位状态。", ""]
        for number, job in enumerate(result["ranked_jobs"], 1):
            page += [f"## {number}. {job['title']} — {job['company']}", "",
                     f"[岗位来源]({job['url']}) · 检索分 {job['retrieval_score']}（非概率）", "",
                     "资料与岗位描述均出现：" + ", ".join(job["matched_skill_mentions"]), "",
                     "岗位提到但资料未体现（不能视为不会）：" + ", ".join(job["job_skill_mentions_not_evidenced_in_profile"]), "",
                     "岗位描述依据：", ""]
            for flag, snippet in job["restriction_mentions_to_verify"].items():
                page += [f"**待核实限制（{flag}，不代表资料持有人满足）**：" + " ".join(snippet.split()), ""]
            for skill, snippet in job["job_evidence"].items():
                page += [f"- **{skill}**：" + " ".join(snippet.split()), ""]
            if job["resume_evidence"]:
                page += ["简历原文依据（不是补写的摘要）：", ""]
                for skill, item in job["resume_evidence"].items():
                    page += [f"- **{skill}**（第 {item['page']} 页）：" + " ".join(item["text"].split()), ""]
        (args.output / f"{cid}.md").write_text("\n".join(page), encoding="utf-8")
        if result["ranked_jobs"]:
            best = result["ranked_jobs"][0]
            cells = [f"[{cid}]({cid}.md)", result["role_family"], f"[{best['title']}]({best['url']})",
                     best["company"], ", ".join(best["matched_skill_mentions"])]
            overview.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    (args.output / "README.md").write_text("\n".join(overview) + "\n", encoding="utf-8")
    print(json.dumps({"profiles": len(profiles), "jobs": len(jobs),
                      "matches": sum(len(r["ranked_jobs"]) for r in results),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
