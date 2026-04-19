"""
Auth Service — Login, logout, registro de usuarios, refresh token.
"""
import uuid, os, logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, String, Boolean, Enum as PgEnum, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.future import select
from passlib.context import CryptContext
from jose import jwt, JWTError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auth-service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://turnos:turnos_secret@postgres:5432/turnos_db")
SECRET_KEY   = os.getenv("SECRET_KEY", "change-me")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
TOKEN_EXPIRE = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

engine     = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
import bcrypt as _bcrypt
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer     = HTTPBearer()

# ─── ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    email           = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name       = Column(String(255), nullable=False)
    role            = Column(String(50), nullable=False, default="secretaria")
    is_active       = Column(Boolean, nullable=False, default=True)
    created_at      = Column(String, server_default=text("NOW()"))

# ─── Schemas ──────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RegisterRequest(BaseModel):
    tenant_id: uuid.UUID
    email: EmailStr
    password: str
    full_name: str
    role: str = "secretaria"

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    full_name: str
    role: str
    tenant_id: str

class UserResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool

# ─── Helpers ──────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def create_access_token(data: dict, expires_minutes: int = TOKEN_EXPIRE) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

# ─── App ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Auth Service iniciado")
    yield

app = FastAPI(title="Auth Service", version="1.0.0", lifespan=lifespan)

# ─── Endpoints ────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.email == body.email)
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario inactivo")

    token = create_access_token({
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role,
        "email": user.email,
    })
    return TokenResponse(
        access_token=token,
        expires_in=TOKEN_EXPIRE * 60,
        user_id=str(user.id),
        full_name=user.full_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
    )

@app.post("/api/auth/register", response_model=UserResponse, status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(User).where(User.email == body.email, User.tenant_id == body.tenant_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="El email ya está registrado en este tenant")

    user = User(
        id=uuid.uuid4(),
        tenant_id=body.tenant_id,
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Usuario registrado: %s (tenant: %s)", user.email, user.tenant_id)
    return UserResponse(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
    )

@app.get("/api/auth/me", response_model=UserResponse)
async def me(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return UserResponse(
        id=user.id, tenant_id=user.tenant_id, email=user.email,
        full_name=user.full_name, role=user.role, is_active=user.is_active,
    )

@app.get("/health")
async def health():
    return {"status": "ok", "service": "auth"}
