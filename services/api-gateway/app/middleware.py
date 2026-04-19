"""
Middleware que extrae y valida el tenant_id del JWT
y lo inyecta como header X-Tenant-ID para todos los servicios downstream.
"""
import os, logging
from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
ALGORITHM  = "HS256"

PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

logger = logging.getLogger("gateway.middleware")


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if request.method == "OPTIONS":
            return await call_next(request)

        # Rutas públicas: auth y health no necesitan tenant
        if path in PUBLIC_PATHS or path.startswith("/api/auth/"):
            return await call_next(request)

        # Extraer JWT del header Authorization
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"detail": "Token de autenticación requerido"}, status_code=401
            )

        token = auth_header.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            tenant_id = payload.get("tenant_id")
            user_id   = payload.get("sub")
            role      = payload.get("role")

            if not tenant_id:
                return JSONResponse({"detail": "Token sin tenant_id"}, status_code=401)

            # Inyectar datos en el request.state
            request.state.tenant_id = tenant_id
            request.state.user_id   = user_id
            request.state.role      = role
            logger.info(f"[TenantMiddleware] tenant_id={tenant_id}, user_id={user_id}")

        except JWTError as e:
            logger.warning("JWT inválido: %s", e)
            return JSONResponse({"detail": "Token inválido o expirado"}, status_code=401)

        response = await call_next(request)
        return response
