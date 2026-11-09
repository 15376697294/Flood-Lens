import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError

from app.config import get_settings
from app.routers.auth import router as auth_router
from app.routers.live_map import router as live_map_router
from app.routers.forum import router as forum_router
from app.routers.ai_chat import router as ai_chat_router
from app.services.auth import initialize_auth
from app.services.ui_pages import PAGE_URLS, render_ui_page
from app.routers import flood_router, i18n_router, routing_router, extension_router

settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.APP_DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    contact={
        "name": "Flood Lens Project Team",
        "description": "UK Flood Risk Query System (Sprint 1 + Beautiful UI)",
    },
    license_info={
        "name": "Sprint 1 - Educational Use",
    },
)

# CORSMiddleware: allow_origins="*" must pair with allow_credentials=False
# (browser spec does not allow Access-Control-Allow-Credentials: true when
# Access-Control-Allow-Origin is a wildcard).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*", "X-Language", "Accept-Language"],
    expose_headers=["X-Language", "X-Response-Time"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def redirect_legacy_ui(request: Request, call_next):
    if request.url.path == "/static/index.html":
        return RedirectResponse("/", status_code=307)
    return await call_next(request)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    # Bypass middleware bookkeeping for preflight OPTIONS — respond as soon as possible
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    process_time_ms = (time.perf_counter() - start_time) * 1000
    response.headers["X-Response-Time"] = f"{process_time_ms:.2f}ms"
    lang = request.headers.get("X-Language", settings.DEFAULT_LANGUAGE)
    response.headers["X-Language"] = lang
    return response


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    """Unify 4xx/5xx HTTPException body structure for the frontend."""
    now = datetime.utcnow().isoformat()
    if isinstance(exc.detail, dict):
        err_val = exc.detail.get("error")
        detail_val = exc.detail.get("detail")
        code_val = exc.detail.get("error_code") or f"HTTP_{exc.status_code}"
        ts_val = exc.detail.get("timestamp") or now
        body = {
            "success": False,
            "error": str(err_val) if err_val is not None else "",
            "detail": detail_val,
            "error_code": str(code_val),
            "timestamp": ts_val,
        }
    else:
        body = {
            "success": False,
            "error": str(exc.detail) if exc.detail is not None else "",
            "detail": None,
            "error_code": f"HTTP_{exc.status_code}",
            "timestamp": now,
        }
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]),
         "message": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={
        "success": False,
        "error": "Invalid input. " + "; ".join(
            f"{error['field']}: {error['message']}" for error in errors
        ),
        "detail": errors,
        "error_code": "VALIDATION_ERROR",
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.exception_handler(Exception)
async def global_uncaught_exception_handler(request: Request, exc: Exception):
    """Fallback handler for unexpected server errors."""
    now = datetime.utcnow().isoformat()
    if settings.DEBUG:
        # Include a short traceback summary in DEBUG mode (safe string rendering)
        import traceback
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=5)
        detail_val = "\n".join(tb_lines)
        message = str(exc)
    else:
        message = "Internal server error. Enable DEBUG=True in .env to see details."
        detail_val = None
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": message,
            "detail": detail_val,
            "error_code": "INTERNAL_ERROR",
            "timestamp": now,
        },
    )


app.include_router(flood_router)
app.include_router(i18n_router)
app.include_router(routing_router)
app.include_router(extension_router)
app.include_router(auth_router)
app.include_router(live_map_router)
app.include_router(forum_router)
app.include_router(ai_chat_router)


@app.on_event("startup")
def initialize_account_tables():
    initialize_auth()


@app.get("/", tags=["Root"], summary="UI Home Page")
def ui_home_page():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(render_ui_page("home", STATIC_DIR))
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "description": settings.APP_DESCRIPTION,
        "ui_home": "/static/index.html",
        "docs_swagger": "/docs",
        "docs_redoc": "/redoc",
        "api_base": "/api/v1",
        "default_language": settings.DEFAULT_LANGUAGE,
        "supported_languages": settings.SUPPORTED_LANGUAGES,
        "endpoints": {
            "flood_risk_query": "POST /api/v1/flood/query",
            "validate_postcode": "GET /api/v1/flood/validate/{postcode}",
            "health_check": "GET /api/v1/flood/health",
            "set_language": "POST /api/v1/i18n/set-language",
            "list_languages": "GET /api/v1/i18n/languages",
            "dijkstra_routing": "POST /api/v1/routing/dijkstra",
            "graph_info": "GET /api/v1/routing/graph/info",
        },
        "reserved_extensions": [
            "GET /api/v1/extension/map-data",
            "GET /api/v1/extension/ea-dataset",
            "POST /api/v1/extension/batch-query",
            "GET /api/v1/extension/frontend-config",
        ],
    }


@app.get("/preparedness", include_in_schema=False)
def legacy_preparedness_page():
    return RedirectResponse("/knowledge", status_code=307)


def register_ui_page(page):
    def endpoint():
        return HTMLResponse(render_ui_page(page, STATIC_DIR))
    app.add_api_route(PAGE_URLS[page], endpoint, methods=["GET"], include_in_schema=False)


for ui_page in PAGE_URLS:
    if ui_page != "home":
        register_ui_page(ui_page)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level="info",
    )
