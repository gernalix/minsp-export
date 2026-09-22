# Security and privacy

This project handles highly sensitive local health data.

- Never commit exports, state databases, normalized databases, browser profiles, cookies, credentials, screenshots containing health data, or authentication material.
- MitID authentication is manual. The exporter must not automate, intercept, replay or persist MitID credentials.
- Captured HTTP request headers are intentionally not stored.
- Generic response capture is restricted to the Min Sundhedsplatform origin. A file served by a separate CDN is accepted only when an explicit download is initiated from an authenticated portal page.
- No synthetic keepalive traffic is generated; server-side session expiry is respected and requires a normal manual MitID login in the still-open export browser.
- URLs written to manifests and normalized outputs redact query parameters whose names look like tokens, auth/session identifiers, codes, tickets, keys or secrets.
- The final ZIP is built from an explicit allowlist. It excludes logs and the browser profile, and contains a sanitized copy of `state.sqlite` rather than browser cookies, storage/session state, credentials or authentication material.
- Raw artifacts are authoritative and immutable inputs. Normalization and packaging read them without deleting or replacing them.
- The crawler is intended for read-only navigation. Account mutations are outside scope.
- Export and browser-profile directories are created with user-only permissions when the filesystem supports POSIX modes.
- Keep the machine and export volume encrypted. Treat backups of the export with the same sensitivity as the source medical record.

If a future site adapter requires a new kind of authenticated request, preserve the read-only boundary and add a focused regression test before enabling it.
