"""Proxy HTTP hacia microservicios downstream."""
import logging
from fastapi import Request
from fastapi.responses import Response
import httpx

logger = logging.getLogger("gateway.proxy")


async def proxy_request(request: Request, service_url: str, path: str) -> Response:
    client: httpx.AsyncClient = request.app.state.http_client

    # Construir URL destino
    url = f"{service_url}{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # Propagar headers relevantes + inyectar contexto de tenant
    headers = dict(request.headers)
    headers.pop("host", None)
    
    # Inyectar tenant_id y user_id desde el request.state (seteado por TenantResolutionMiddleware)
    if hasattr(request.state, "tenant_id") and request.state.tenant_id:
        headers["X-Tenant-ID"] = str(request.state.tenant_id)
    if hasattr(request.state, "user_id") and request.state.user_id:
        headers["X-User-ID"] = str(request.state.user_id)
    if hasattr(request.state, "role") and request.state.role:
        headers["X-User-Role"] = str(request.state.role)

    body = await request.body()

    try:
        upstream = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
        logger.debug(f"[Proxy] {request.method} {url} -> {upstream.status_code}")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=dict(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )
    except httpx.ConnectError:
        logger.error("Servicio no disponible: %s", service_url)
        return Response(
            content=b'{"detail":"Servicio no disponible temporalmente"}',
            status_code=503,
            media_type="application/json",
        )
    except Exception as e:
        logger.exception("Error en proxy hacia %s: %s", service_url, e)
        return Response(
            content=b'{"detail":"Error interno del gateway"}',
            status_code=500,
            media_type="application/json",
        )
