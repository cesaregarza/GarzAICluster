"""Run the existing Sendou converter without the retired application's startup."""

import runpy

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importing fast_api_app.routes would eagerly import the database-backed routers.
# This file and shared_lib.turbo_stream come from the digest-pinned API image.
sendou = runpy.run_path("/app/src/fast_api_app/routes/sendou_proxy.py")

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(sendou["router"])


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}
