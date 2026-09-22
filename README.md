# minsp-export

Local, resumable exporter for **Min Sundhedsplatform / Epic MyChart**.

The exporter is designed to archive the health information that is visible to the authenticated user without automating or bypassing MitID. It opens a persistent Chrome profile, waits for a normal user login when needed, crawls read-only pages, captures response payloads, and builds local searchable outputs.

## Outputs

By default the export root is `~/Documents/MinSP/export`:

```text
export/
├── raw/
│   ├── html/
│   ├── json/
│   ├── pdf/
│   └── attachments/
├── normalized/
│   └── health.sqlite
├── text/
│   └── complete-medical-record.md
├── manifests/
│   └── files.jsonl
├── logs/
└── state.sqlite
```

The normalized database contains a generic record layer plus dedicated tables for lab results, clinical notes, encounters, imaging reports, diagnoses, medications, allergies, appointments, messages, procedures, questionnaires, documents, providers and departments. An FTS5 index makes the captured text searchable.

## Security model

- MitID is always completed manually by the user in the real browser.
- The project does not store MitID credentials, cookies in the repository, request headers, authorization headers or passwords.
- The persistent browser profile and export directory are created with user-only permissions.
- Crawling is navigation-only: the crawler follows same-origin MyChart URLs and does not submit forms.
- Obviously destructive/action routes such as logout, delete, cancel, payment, compose/send and scheduling workflows are excluded.
- Captured material is health data. Keep the export local and encrypted; never commit `raw/`, `*.sqlite` or browser profiles.

## Requirements

- Python 3.11+
- Google Chrome stable
- Python package `playwright`
- optional: `pdftotext` (Poppler) for searchable PDF text

Install the package using the system/user Python policy you already use on Fedora; this repository does not require or create a virtual environment.

## Usage

```bash
# From the repository root
python3 -m pip install --user -e .
minsp-export login
minsp-export export

# Resume an interrupted crawl
minsp-export crawl

# Rebuild normalized outputs without revisiting the site
minsp-export normalize
minsp-export render

# Inspect progress and search the local normalized archive
minsp-export status
minsp-export search ferritin
```

`minsp-export login` opens the dedicated persistent browser profile. Complete MitID normally. `export` can also wait for interactive login itself.

For unattended/incremental runs use:

```bash
minsp-export crawl --non-interactive
```

If authentication is required it exits with code **10** instead of attempting to automate MitID. A later authenticated run resumes from the checkpoint.

## Completeness

The crawler stores every page and supported network payload it observes and can resume safely, but MyChart installations differ and some sections are JavaScript-driven. The repository therefore separates generic capture from site-specific adapters. A real authenticated reconnaissance pass is required to prove coverage of every section exposed by this Min Sundhedsplatform deployment. That live verification belongs in the Codex roadmap task generated for this project.

Also note that “everything Min Sundhedsplatform exposes” is not necessarily the complete hospital record; information not made available in the portal cannot be recovered by this exporter.
