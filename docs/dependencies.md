# Dependencies

Runtime: `fastapi`, `uvicorn`, `pydantic` (the agreed skeleton set). SQLite is used via the
standard library `sqlite3`, no ORM.

Dev-only additions beyond that set:

- `pytest`: the test runner.
- `httpx`: required by `fastapi.testclient.TestClient` (Starlette's test client is built on
  it); test-only, no runtime use.
