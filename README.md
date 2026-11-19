# JobMatch

JobMatch collects public job postings, retrieves relevant job-description passages, and uses OpenAI or Gemini to compare a resume against the shortlisted jobs. The web app runs locally.

## Setup

Use Python 3.11 or newer. From the repository root, run these PowerShell commands. The application uses the Python standard library.

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python -m jobmatch_data collect
& $python -m jobmatch_data stats
```

Collection creates `data/jobs.sqlite3`. Check that `with_description` is greater than zero before starting the app. If a source fails, the command reports the error and retains records from the sources that succeeded. You can also import a normalized JSON array with `& $python -m jobmatch_data import path/to/jobs.json --source local`. Each job needs `id`, `title`, `company`, `url`, and a nonempty `description` for matching.

## Start the web app

In the same PowerShell session, set your API key and start the server. Enter the key at the prompt. Keep API keys in local environment variables or local credential files.

```powershell
$env:OPENAI_API_KEY = Read-Host 'OpenAI API key'
& $python -m jobmatch_data serve --database data/jobs.sqlite3
```

Use `--model MODEL_ID` to select a model available to your account. For Gemini, set `GEMINI_API_KEY` and start the server with `--provider gemini`.

Open [JobMatch](http://127.0.0.1:8000). Paste a resume or upload a PDF, Markdown or text file and select **Find matching jobs**. PDF text is extracted in the browser with PDF.js, which loads from jsDelivr the first time you choose a PDF. The PDF file is not uploaded. A scanned PDF without selectable text cannot be read; paste its text instead. The app sends relevant resume excerpts and job descriptions to the configured model provider.

## New US jobs

Open [New US jobs](http://127.0.0.1:8000/new) from the same server. It is separate from the archive match above and uses its own database, `data/latest_jobs.sqlite3`.

- While `serve` runs, a background refresh downloads recent US postings from the SimplifyJobs new-grad and internship lists at startup and every 30 minutes. It fetches job descriptions from public Greenhouse, Lever and Ashby job APIs. Select **Refresh now** to start one immediately. A refresh makes no model calls.
- Save one resume, choose internships, full-time or both, and tick **I need visa sponsorship** if it applies. Then select **Find new jobs**. The page shows the 10 best technical matches from the last 7 days (14 days if fewer than 10 qualify), each with a link, degree and experience requirements, a visa note and matched skills.
- With an API key, a ranking agent searches and reads job descriptions before choosing the 10 jobs. Without one, the page uses keyword ranking and says so.
- To refresh once from the command line, run `& $python -m jobmatch_data latest-refresh`.

### Evaluate New US jobs

The evaluation compares keyword ranking, scoring every job with the model (`score_all`), the agent and the agent with a smaller read budget (`agent_low`) on four fictional resumes in `eval/resumes/`. It runs on a frozen copy of the jobs database so that every method sees the same jobs.

```powershell
& $python -m jobmatch_data latest-eval snapshot
& $python -m jobmatch_data latest-eval run data/latest_eval/snapshots/SNAPSHOT.sqlite3 --methods keyword,score_all,agent,agent_low --repeats 3
& $python -m jobmatch_data latest-eval labels data/latest_eval/runs/RUN.json
& $python -m jobmatch_data latest-eval report data/latest_eval/runs/RUN.json data/latest_eval/runs/RUN.labels.csv
```

`labels` writes one row per resume and job that any method placed in its top 10. Put `1` (good match) or `0` (poor match) in the `label` column, then run `report`. It prints precision at 10, constraint violations, model calls, tokens, time, fallback rate and the agent's tool error rate for each method, and saves the table next to the run. Rerunning `labels` keeps labels you have already entered. Methods other than `keyword` need an API key, set up as for `serve`.

Add `--inject` to `run` to test prompt injection. For each resume, the job that keyword ranking likes least gets a note telling AI systems to rank it first; the note is added to a temporary copy of the snapshot, never to the snapshot itself. The report's `injection_success` column is the share of model runs that put that job in the top 10. With `--repeats` above 1, `stability` is the average overlap (Jaccard) of the top-10 lists across repeats.

## Match from the command line

```powershell
& $python -m jobmatch_data match path/to/resume.md --database data/jobs.sqlite3
```

Reports are written under `data/matches/`. If you use the optional local resume library for sample selection or reverse matching, put its metadata at `data/candidates/full_resumes_v1/profiles.json`. The metadata is a JSON array of objects with `candidate_id` and `headline` fields. Put each resume at `markdown/<candidate_id>.md` within the same library directory.

## Repository contents

Git includes the application files under `src/jobmatch_data/`, `pyproject.toml`, `README.md`, and `.gitignore`. Local credentials, job and resume data, reports, diagrams, proposals, tests, and temporary files are excluded. If a credential has already been committed, rotate it and remove it from the repository history before publishing.

To publish JobMatch on its own, initialize a repository in the JobMatch directory. Check that `git rev-parse --show-toplevel` points to that directory. Review the staged file list before committing.

```powershell
git init
git rev-parse --show-toplevel
git add -- .gitignore README.md pyproject.toml src/jobmatch_data
git diff --cached --name-only
```
