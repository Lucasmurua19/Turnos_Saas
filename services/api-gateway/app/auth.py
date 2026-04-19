import os
from fastapi import Security, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
ALGORITHM  = "HS256"
bearer     = HTTPBearer()


def verify_token(request: Request):
    """
    Verifica que el request.state tenga tenant_id (previamente seteado por TenantResolutionMiddleware).
    Si el middleware no lo seteó, significa que el token es inválido.
    """
    if not hasattr(request.state, 'tenant_id') or not request.state.tenant_id:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return request.state
