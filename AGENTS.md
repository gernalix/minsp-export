# minsp-export

## Purpose
This repository builds a resumable local archive from read-only pages available to the authenticated Min Sundhedsplatform user. MegaVault owns project identity and repository location; C2 owns task lifecycle. The live portal, MitID and Chrome are external user-controlled systems.

## Architecture and data
`src/minsp_export/cli.py` exposes the CLI. `browser.py` manages the persistent Chrome profile, `crawler.py` captures same-origin pages and supported payloads, `storage.py` checkpoints raw artifacts, and `normalize.py`, `render.py` and `archive.py` build local projections and allowlisted output. Raw captures remain authoritative. See `README.md`, `SECURITY.md` and `docs/REAL_VALIDATION.md`.

## Privacy and safe validation
Captured material is sensitive health data. Never commit exports, SQLite files, browser profiles, credentials, cookies, request headers or authentication material. Unit tests use temporary directories and mocked browser/network objects; run them with `PYTHONPATH=src PYTHONWARNINGS=error python3 -B -m unittest discover -s tests -q`. Do not open the real portal, launch the user's browser profile, run an export/crawl or inspect live health data during source validation. MitID is always completed manually. Do not submit forms or perform account mutations. Keep export storage local and encrypted.
