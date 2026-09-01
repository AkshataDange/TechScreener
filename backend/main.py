from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routers import admin, auth, interview, review, user
from auth_service import ensure_default_admin
import database as db

BACKEND_DIR = Path(__file__).parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on startup — initialises the database."""
    db.init_db()
    ensure_default_admin()
    # Assignments and completed reports must survive backend restarts.
    yield


app = FastAPI(
    title=settings.app_name,
    description="AI-powered L1 technical interview system",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(interview.router)
app.include_router(review.router)
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
def serve_root():
    return RedirectResponse(url="/user", status_code=307)


@app.get("/practice", include_in_schema=False)
def serve_frontend():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/reviewer", include_in_schema=False)
def serve_reviewer():
    return FileResponse(str(FRONTEND_DIR / "admin.html"))


@app.get("/user", include_in_schema=False)
def serve_user():
    return FileResponse(str(FRONTEND_DIR / "user.html"))


@app.get("/admin", include_in_schema=False)
def serve_admin():
    return FileResponse(str(FRONTEND_DIR / "admin.html"))


@app.get("/admin/users-page", include_in_schema=False)
def serve_admin_users():
    return FileResponse(str(FRONTEND_DIR / "admin-users.html"))


@app.get("/admin/assign", include_in_schema=False)
def serve_admin_assign():
    return FileResponse(str(FRONTEND_DIR / "admin-assign.html"))


@app.get("/admin/reports-page", include_in_schema=False)
def serve_admin_reports():
    return FileResponse(str(FRONTEND_DIR / "admin-reports.html"))


@app.get("/health", tags=["system"])
def health_check():
    return {"status": "ok", "app": settings.app_name, "debug": settings.debug}
