"""
API Gateway — Punto de entrada único del SaaS de turnos.
Resuelve el tenant, valida el JWT y hace proxy a los microservicios.
"""
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis
import os, logging

from .middleware import TenantResolutionMiddleware
from .proxy import proxy_request
from .auth import verify_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

SERVICES = {
    "auth":        os.getenv("AUTH_SERVICE_URL", "http://auth-service:8001"),
    "tenants":     os.getenv("TENANT_SERVICE_URL", "http://tenant-service:8002"),
    "appointments":os.getenv("APPOINTMENT_SERVICE_URL", "http://appointment-service:8003"),
    "notifications":os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8004"),
    "messaging":   os.getenv("MESSAGING_SERVICE_URL", "http://messaging-service:8005"),
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    app.state.redis = await aioredis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379"), decode_responses=True
    )
    logger.info("Gateway listo. Servicios: %s", list(SERVICES.keys()))
    yield
    await app.state.http_client.aclose()
    await app.state.redis.aclose()

app = FastAPI(
    title="Turnos SaaS — API Gateway",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(TenantResolutionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ─── Rutas públicas (sin JWT) ─────────────────────────────────────

@app.api_route("/api/auth/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def route_auth(path: str, request: Request):
    target_path = f"/api/auth/{path}" if path else "/api/auth"
    return await proxy_request(request, SERVICES["auth"], target_path)

# ─── Rutas protegidas ─────────────────────────────────────────────

@app.api_route("/api/tenants/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def route_tenants(path: str, request: Request, _=Depends(verify_token)):
    target_path = f"/api/tenants/{path}" if path else "/api/tenants"
    return await proxy_request(request, SERVICES["tenants"], target_path)

@app.api_route("/api/appointments/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def route_appointments(path: str, request: Request, _=Depends(verify_token)):
    target_path = f"/api/appointments/{path}" if path else "/api/appointments"
    return await proxy_request(request, SERVICES["appointments"], target_path)

@app.api_route("/api/notifications/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def route_notifications(path: str, request: Request, _=Depends(verify_token)):
    target_path = f"/api/notifications/{path}" if path else "/api/notifications"
    return await proxy_request(request, SERVICES["notifications"], target_path)

@app.api_route("/api/messaging/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def route_messaging(path: str, request: Request, _=Depends(verify_token)):
    target_path = f"/api/messaging/{path}" if path else "/api/messaging"
    return await proxy_request(request, SERVICES["messaging"], target_path)

@app.get("/health")
async def health():
    statuses = {}
    for name, url in SERVICES.items():
        try:
            r = await app.state.http_client.get(f"{url}/health", timeout=3.0)
            statuses[name] = "ok" if r.status_code == 200 else "degraded"
        except Exception:
            statuses[name] = "unreachable"
    return {"gateway": "ok", "services": statuses}
