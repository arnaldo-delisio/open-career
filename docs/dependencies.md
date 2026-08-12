# Dependencies

Runtime: `fastapi`, `uvicorn`, `pydantic` (the agreed skeleton set). SQLite is used via the
standard library `sqlite3`, no ORM.

- `playwright`: the CV render path (content model to HTML to headless Chromium PDF, the
  career-ops pipeline shape adopted as an idea with attribution, OC-33/OC-34). Chromium
  print-to-PDF is the ATS-proven engine; LaTeX was rejected at design time as a heavyweight
  operator toolchain. Browsers install via `uv run playwright install chromium`.

System (not pip): `pdftotext` (poppler-utils), a hard dependency of both onboarding PDF
ingestion and the mandatory package ATS-parseability check; there is no degraded mode.

Dev-only additions beyond that set:

- `pytest`: the test runner.
- `httpx`: required by `fastapi.testclient.TestClient` (Starlette's test client is built on
  it); test-only, no runtime use.
