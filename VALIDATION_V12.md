# Biology V12 validation

- Based on the biology ZIP attached in this conversation. Biology curriculum files and Render environment definitions are byte-identical to the attachment.
- 206 tests passed: application construction, assessment policies, parent messaging, schedule packing and existing regressions.
- Integration tests execute SQL in isolated PGlite (tests/package-lock.json); verify grade 79 warns once, grade 80 removes that failure warning, and future schedule migration preserves published content and exam links across restart.
- Telegram calls are mocked. No live Telegram session, Render deployment or production PostgreSQL database was used.
- Run: python -m pip install -r requirements-dev.txt; npm ci --prefix tests; python -m pytest -q tests.
- Schedule: Sunday 1, Monday 1, Tuesday 2. A single remaining chapter-ending lesson stays alone; teacher can edit daily counts, dates, course name and lecture content.
