"""
Tenant Service — Alta, gestión y configuración de consultorios (tenants).
"""
import uuid, os, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, String, Boolean, text
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.future import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tenant-service")

DATABASE_URL = os.getenv("DATABASE_URL","postgresql+asyncpg://turnos:turnos_secret@postgres:5432/turnos_db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ─── ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase): pass

class Tenant(Base):
    __tablename__ = "tenants"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug       = Column(String(100), unique=True, nullable=False)
    name       = Column(String(255), nullable=False)
    email      = Column(String(255), nullable=False)
    phone      = Column(String(50))
    plan       = Column(String(20), nullable=False, default="basic")
    status     = Column(String(20), nullable=False, default="active")
    settings   = Column(JSONB, default={})
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))
    updated_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))

# ─── Schemas ──────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    slug:  str
    name:  str
    email: EmailStr
    phone: Optional[str] = None
    plan:  str = "basic"

class TenantUpdate(BaseModel):
    name:     Optional[str] = None
    email:    Optional[EmailStr] = None
    phone:    Optional[str] = None
    plan:     Optional[str] = None
    settings: Optional[dict] = None

class TenantResponse(BaseModel):
    id:       uuid.UUID
    slug:     str
    name:     str
    email:    str
    phone:    Optional[str] = None
    plan:     str
    status:   str
    settings: dict = {}
    created_at: datetime
    class Config: from_attributes = True

# ─── Helpers ──────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def require_superadmin(x_user_role: str = Header(...)):
    if x_user_role != "superadmin":
        raise HTTPException(403, "Solo superadmin puede gestionar tenants")

# ─── App ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Tenant Service iniciado")
    yield

app = FastAPI(title="Tenant Service", version="1.0.0", lifespan=lifespan)

@app.post("/api/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(body: TenantCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Slug '{body.slug}' ya está en uso")
    tenant = Tenant(id=uuid.uuid4(), **body.model_dump())
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    logger.info("Tenant creado: %s (%s)", tenant.name, tenant.slug)
    return tenant

@app.get("/api/tenants", response_model=List[TenantResponse])
async def list_tenants(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    return r.scalars().all()

@app.get("/api/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    t = r.scalar_one_or_none()
    if not t: raise HTTPException(404, "Tenant no encontrado")
    return t

@app.patch("/api/tenants/{tenant_id}", response_model=TenantResponse)
async def update_tenant(tenant_id: uuid.UUID, body: TenantUpdate, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    t = r.scalar_one_or_none()
    if not t: raise HTTPException(404, "Tenant no encontrado")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(t)
    return t

@app.patch("/api/tenants/{tenant_id}/status")
async def toggle_tenant_status(
    tenant_id: uuid.UUID,
    action: str,  # "activate" | "suspend"
    db: AsyncSession = Depends(get_db)
):
    r = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    t = r.scalar_one_or_none()
    if not t: raise HTTPException(404, "Tenant no encontrado")
    t.status = "active" if action == "activate" else "suspended"
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": str(tenant_id), "status": t.status}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "tenants"}
