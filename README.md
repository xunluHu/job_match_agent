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
