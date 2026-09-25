"""One error shape for every failure the API reports.

The UI renders ``message`` as the headline and ``advice`` as what to do next, so every
error raised here is written for the person using the product, not for a log file.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        advice: str | None = None,
        detail: str | None = None,
        field: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.advice = advice
        self.detail = detail
        self.field = field

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "advice": self.advice,
            "detail": self.detail,
            "field": self.field,
        }


def not_found(what: str) -> ApiError:
    return ApiError(
        404,
        "not_found",
        f"{what} was not found.",
        "It may have expired after a server restart. Upload the workbook again.",
    )


def conflict(message: str, advice: str | None = None) -> ApiError:
    return ApiError(409, "conflict", message, advice)


async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"error": error.as_dict()})


async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": first.get("msg", "The request is not valid."),
                "advice": None,
                "detail": None,
                "field": location or None,
            }
        },
    )
