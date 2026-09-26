"""FastAPI application.

    .venv\\Scripts\\python -m uvicorn api.main:app --reload --port 8000

In development the Vite dev server proxies ``/api`` here. After ``npm run build`` the
compiled UI in ``frontend/dist`` is served from the same origin.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .errors import ApiError, api_error_handler, validation_error_handler
from .routes import batches, exports, jobs, workbooks
from .services import sheet_pool

@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Cleaning worker processes outlive requests; stop them with the service.
    sheet_pool.shutdown()


app = FastAPI(title="Exavalu Data Cleaning Studio API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)

app.include_router(workbooks.router)
app.include_router(jobs.router)
app.include_router(exports.router)
app.include_router(batches.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "max_upload_mb": config.MAX_UPLOAD_MB,
            "max_batch_files": config.MAX_BATCH_FILES,
            "clean_workers": config.CLEAN_WORKERS,
            "accepted": sorted(config.ALLOWED_SUFFIXES)}


if config.FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        target = config.FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(config.FRONTEND_DIST / "index.html")
