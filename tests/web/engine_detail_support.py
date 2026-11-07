"""Reading back everything a request left behind, to prove Docker's own text is not in it."""

import io
import json

from fastapi import FastAPI


async def recorded_text(app: FastAPI, log_stream: io.StringIO) -> str:
    """Return every activity row and every captured log line as one string."""
    async with app.state.database.session() as db:
        rows = await db.fetch_limited("SELECT * FROM activity_event", (), limit=1000)
    return json.dumps([dict(row) for row in rows]) + log_stream.getvalue()
