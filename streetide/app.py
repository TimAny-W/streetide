"""Streetide web app."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from streetide.tide import flood

STATIC = Path(__file__).parent / "static"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logging.getLogger("streetide").setLevel(logging.INFO)

app = FastAPI(title="Streetide", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class TideRequest(BaseModel):
    lat: float = Field(..., ge=-85, le=85)
    lng: float = Field(..., ge=-180, le=180)
    minutes: float = Field(15, ge=1, le=25)
    mode: str = Field("walk")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/tide")
def api_tide(body: TideRequest):
    logging.getLogger("streetide").info("POST /api/tide %s %s min", body.mode, body.minutes)
    try:
        return flood(body.lat, body.lng, body.minutes, body.mode)
    except Exception as exc:
        logging.getLogger("streetide").exception("tide failed")
        raise HTTPException(status_code=502, detail=f"Could not flood this spot: {exc}") from exc


def main():
    import uvicorn

    uvicorn.run("streetide.app:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
