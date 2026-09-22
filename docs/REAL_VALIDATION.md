# Real authenticated validation contract

Generic capture is intentionally deployment-agnostic. The remaining proof of completeness must be performed against the real authenticated Min Sundhedsplatform account on the user's Fedora machine.

The live validator must:

1. register or resolve this repository in the local MegaVault and use its canonical project_id;
2. install or use dependencies without creating any virtual environment;
3. run the focused unit tests first;
4. launch the dedicated persistent Chrome profile and let the user complete MitID manually;
5. inventory every authenticated top-level section, route and read-only lazy-load or pagination control;
6. compare that inventory with the URLs and network payloads captured by the exporter;
7. add only the minimum site-specific adapters or selectors needed for sections the generic crawler misses;
8. never automate MitID, read credentials, submit forms, send messages, book or cancel appointments, request refills, make payments, change preferences, or perform any other account mutation;
9. run a complete export to exhaustion with checkpoint and resume tested by one deliberate interruption;
10. verify raw HTML, JSON, PDF and attachments, normalized SQLite tables, FTS5 search and the combined Markdown output;
11. report any portal data category that is unavailable to the user as an explicit coverage limitation rather than fabricating completeness.

PASS requires evidence that every section exposed by the authenticated portal was either captured or explicitly documented as inaccessible or non-exportable.
