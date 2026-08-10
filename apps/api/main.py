"""FastAPI app: health endpoint plus repository wiring.

Run: uv run uvicorn apps.api.main:app
"""

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException

from adapters.storage.instance import db_path
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from domain.ports import CareerEdgeRepository

app = FastAPI(title="open-career")


def get_edge_repository() -> Iterator[CareerEdgeRepository]:
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=503, detail="instance not initialized (run: open-career init)")
    conn = sqlite3.connect(path)
    try:
        yield SqliteCareerEdgeRepository(conn)
    finally:
        conn.close()


@app.get("/health")
def health(repo: CareerEdgeRepository = Depends(get_edge_repository)) -> dict:
    return {"status": "ok", "edges": len(repo.list_all())}
