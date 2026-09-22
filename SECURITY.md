# Security and privacy

This project handles highly sensitive local health data.

- Never commit exports, state databases, normalized databases, browser profiles, cookies, credentials, screenshots containing health data, or authentication material.
- MitID authentication is manual. The exporter must not automate, intercept, replay or persist MitID credentials.
- Captured HTTP request headers are intentionally not stored.
- URLs written to manifests and normalized outputs redact query parameters whose names look like tokens, auth/session identifiers, codes, tickets, keys or secrets.
- The crawler is intended for read-only navigation. Account mutations are outside scope.
- Export and browser-profile directories are created with user-only permissions when the filesystem supports POSIX modes.
- Keep the machine and export volume encrypted. Treat backups of the export with the same sensitivity as the source medical record.

If a future site adapter requires a new kind of authenticated request, preserve the read-only boundary and add a focused regression test before enabling it.
