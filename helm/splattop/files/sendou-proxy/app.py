"""Forward Sendou JSON without starting the retired SplatTop application."""

import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from sendou_response import (
    MATCH_ROUTE,
    QUEUE_ROUTE,
    TEAM_ROUTE,
    TOURNAMENT_ROUTE,
    extract_route,
    extract_teams,
    loader_url,
)

logger = logging.getLogger(__name__)
SENDOU_BASE_URL = "https://sendou.ink"

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


async def fetch_route(path, route_key):
    url = loader_url(SENDOU_BASE_URL + path + ".data")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.warning("Sendou returned %s for %s", error.response.status_code, path)
        raise HTTPException(
            status_code=error.response.status_code,
            detail=f"sendou.ink returned {error.response.status_code}",
        ) from error
    except httpx.RequestError as error:
        logger.warning("Failed to fetch Sendou route %s", path)
        raise HTTPException(status_code=502, detail="Failed to fetch from sendou.ink") from error
    try:
        return extract_route(response.json(), route_key)
    except ValueError as error:
        logger.warning("Invalid Sendou JSON response for %s: %s", path, error)
        raise HTTPException(status_code=502, detail="Invalid JSON response from sendou.ink") from error


@app.get("/api/sendou/to/{tournament_id}/matches/{match_id}")
async def get_tournament_match(tournament_id: int, match_id: int):
    return await fetch_route(f"/to/{tournament_id}/matches/{match_id}", MATCH_ROUTE)


@app.get("/api/sendou/to/{tournament_id}/teams/{team_id}")
async def get_tournament_team(tournament_id: int, team_id: int):
    return await fetch_route(f"/to/{tournament_id}/teams/{team_id}", TEAM_ROUTE)


@app.get("/api/sendou/to/{tournament_id}/teams")
async def get_tournament_teams(tournament_id: int):
    data = await fetch_route(f"/to/{tournament_id}", TOURNAMENT_ROUTE)
    try:
        return extract_teams(data)
    except ValueError as error:
        logger.warning("Invalid Sendou team list for tournament %s", tournament_id)
        raise HTTPException(status_code=502, detail="Invalid team list from sendou.ink") from error


@app.get("/api/sendou/q/match/{match_id}")
async def get_queue_match(match_id: int):
    return await fetch_route(f"/q/match/{match_id}", QUEUE_ROUTE)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}
