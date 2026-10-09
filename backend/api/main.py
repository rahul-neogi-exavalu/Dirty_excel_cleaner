"""FastAPI application.

    .venv\\Scripts\\python -m uvicorn api.main:app --reload --port 8080

Run from ``backend/`` (or add ``--app-dir backend`` from the project root). In
development the Vite dev server proxies ``/api`` here (or to ``VITE_API_TARGET``). After ``npm run build`` the
compiled UI in ``frontend/dist`` is served from the same origin.

Every ``/api`` route needs a signed-in user except ``/api/health`` and sign-in itself.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import app_db, auth, config, db
from .errors import ApiError, api_error_handler, validation_error_handler
from .routes import auth as auth_routes
from .routes import batches, bronze, exports, history, jobs, silver, users, validation, workbooks
from .services import job_history, sheet_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Jobs left queued or running belonged to the previous process; they will not finish.
    job_history.sweep_interrupted()
    yield
    # Cleaning worker processes outlive requests; stop them with the service.
    sheet_pool.shutdown()
    job_history.flush(timeout=5)
    db.close()
    app_db.close()


app = FastAPI(title="Exavalu Data Processing Studio API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)

# Sign-in, sign-out and "who am I" guard themselves; everything else needs a user.
app.include_router(auth_routes.router)
signed_in = [Depends(auth.require_user)]
for router in (workbooks.router, jobs.router, exports.router, batches.router, validation.router, bronze.router,
               silver.router, history.router, users.router):
    app.include_router(router, dependencies=signed_in)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "max_upload_mb": config.MAX_UPLOAD_MB,
            "max_batch_files": config.MAX_BATCH_FILES,
            "clean_workers": config.CLEAN_WORKERS,
            "accepted": sorted(config.ALLOWED_SUFFIXES),
            "sign_in": config.APP_DB_CONFIGURED}


if config.FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        target = config.FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(config.FRONTEND_DIST / "index.html")
