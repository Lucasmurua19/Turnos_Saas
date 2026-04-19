"""
Messaging Service — Envío de emails con SendGrid o SMTP.
Modo mock para desarrollo (EMAIL_MOCK_MODE=true).
Registra cada envío en la tabla messages.
"""
import uuid, os, logging, smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import Column, String, Text, text
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.future import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("messaging-service")

DATABASE_URL    = os.getenv("DATABASE_URL", "postgresql+asyncpg://turnos:turnos_secret@postgres:5432/turnos_db")
SENDGRID_KEY    = os.getenv("SENDGRID_API_KEY", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM", "noreply@turnossaas.com")
EMAIL_MOCK_MODE = os.getenv("EMAIL_MOCK_MODE", "true").lower() == "true"
SMTP_HOST       = os.getenv("SMTP_HOST", "")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER       = os.getenv("SMTP_USER", "")
SMTP_PASS       = os.getenv("SMTP_PASS", "")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ─── ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase): pass

class Message(Base):
    __tablename__ = "messages"
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), nullable=False)
    appointment_id = Column(UUID(as_uuid=True))
    patient_id     = Column(UUID(as_uuid=True), nullable=False)
    channel        = Column(String(20), default="email")
    template_key   = Column(String(100))
    subject        = Column(String(255))
    body           = Column(Text, nullable=False)
    status         = Column(String(30), default="pending")
    external_id    = Column(String(255))
    sent_at        = Column(TIMESTAMP(timezone=True))
    error_detail   = Column(Text)
    created_at     = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))
    updated_at     = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))

# ─── Templates ────────────────────────────────────────────────────

TEMPLATES: dict[str, dict] = {
    "reminder": {
        "subject": "Recordatorio de turno — {date}",
        "html": """
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px">
  <h2 style="color:#1a56db">Recordatorio de turno médico</h2>
  <p>Hola <strong>{patient_name}</strong>,</p>
  <p>Te recordamos que tenés un turno programado:</p>
  <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:16px 0">
    <p style="margin:4px 0"><strong>Médico:</strong> {professional_name}</p>
    <p style="margin:4px 0"><strong>Fecha:</strong> {date}</p>
  </div>
  <p>Respondé a este email con:</p>
  <ul>
    <li><strong>1</strong> — Confirmar asistencia</li>
    <li><strong>2</strong> — Cancelar turno</li>
  </ul>
  <p style="color:#6b7280;font-size:12px">ID de turno: {appointment_id}</p>
</div>""",
    },
    "confirmation": {
        "subject": "Turno confirmado ✓",
        "html": """
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px">
  <h2 style="color:#059669">Turno confirmado</h2>
  <p>Hola <strong>{patient_name}</strong>, tu turno del <strong>{date}</strong>
  con <strong>{professional_name}</strong> fue confirmado. ¡Te esperamos!</p>
</div>""",
    },
    "cancellation": {
        "subject": "Turno cancelado",
        "html": """
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px">
  <h2 style="color:#dc2626">Turno cancelado</h2>
  <p>Hola <strong>{patient_name}</strong>, tu turno del <strong>{date}</strong>
  fue cancelado. Contactá al consultorio para reprogramar.</p>
</div>""",
    },
}

def render_template(key: str, variables: dict) -> tuple[str, str]:
    """Devuelve (subject, html_body) con variables interpoladas."""
    tpl = TEMPLATES.get(key)
    if not tpl:
        return variables.get("subject", "Mensaje"), variables.get("body", "")
    subject = tpl["subject"].format(**variables)
    html    = tpl["html"].format(**{k: v or "" for k, v in variables.items()})
    return subject, html

# ─── Envío real ───────────────────────────────────────────────────

async def send_email_sendgrid(to: str, subject: str, html: str) -> str:
    """Envía vía SendGrid HTTP API. Devuelve message_id."""
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": to}], "subject": subject}],
                "from": {"email": EMAIL_FROM},
                "content": [{"type": "text/html", "value": html}],
            },
            timeout=15.0,
        )
        if r.status_code not in (200, 202):
            raise RuntimeError(f"SendGrid error {r.status_code}: {r.text}")
        return r.headers.get("X-Message-Id", str(uuid.uuid4()))

def send_email_smtp(to: str, subject: str, html: str) -> str:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, to, msg.as_string())
    return str(uuid.uuid4())

# ─── Schemas ──────────────────────────────────────────────────────

class SendRequest(BaseModel):
    tenant_id:      str
    appointment_id: Optional[str] = None
    patient_id:     str
    channel:        str = "email"
    template_key:   Optional[str] = None
    to_email:       Optional[str] = None
    subject:        Optional[str] = None
    body:           Optional[str] = None
    variables:      dict = {}

class MessageResponse(BaseModel):
    id:     str
    status: str
    mock:   bool = False

# ─── App ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    mode = "MOCK" if EMAIL_MOCK_MODE else ("SendGrid" if SENDGRID_KEY else "SMTP")
    logger.info("Messaging Service iniciado. Modo: %s", mode)
    yield

app = FastAPI(title="Messaging Service", version="1.0.0", lifespan=lifespan)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@app.post("/api/messaging/send", response_model=MessageResponse, status_code=201)
async def send_message(body: SendRequest, db: AsyncSession = Depends(get_db)):
    # Renderizar template si se proporcionó clave
    if body.template_key:
        subject, html_body = render_template(body.template_key, body.variables)
    else:
        subject  = body.subject or "Mensaje"
        html_body = body.body or ""

    # Persistir el mensaje
    msg = Message(
        id             = uuid.uuid4(),
        tenant_id      = uuid.UUID(body.tenant_id),
        appointment_id = uuid.UUID(body.appointment_id) if body.appointment_id else None,
        patient_id     = uuid.UUID(body.patient_id),
        channel        = body.channel,
        template_key   = body.template_key,
        subject        = subject,
        body           = html_body,
        status         = "pending",
    )
    db.add(msg)
    await db.commit()

    # Enviar
    if EMAIL_MOCK_MODE:
        logger.info("[MOCK] Email → %s | Subject: %s", body.to_email, subject)
        msg.status      = "sent"
        msg.sent_at     = datetime.now(timezone.utc)
        msg.external_id = f"mock-{msg.id}"
        await db.commit()
        return MessageResponse(id=str(msg.id), status="sent", mock=True)

    try:
        if SENDGRID_KEY and SENDGRID_KEY != "mock":
            ext_id = await send_email_sendgrid(body.to_email, subject, html_body)
        elif SMTP_HOST:
            ext_id = send_email_smtp(body.to_email, subject, html_body)
        else:
            raise RuntimeError("No hay proveedor de email configurado")

        msg.status      = "sent"
        msg.sent_at     = datetime.now(timezone.utc)
        msg.external_id = ext_id
        logger.info("Email enviado a %s (id: %s)", body.to_email, ext_id)
    except Exception as e:
        msg.status       = "failed"
        msg.error_detail = str(e)
        logger.error("Error enviando email: %s", e)
        await db.commit()
        raise HTTPException(500, f"Error enviando mensaje: {e}")

    await db.commit()
    return MessageResponse(id=str(msg.id), status=msg.status)

@app.get("/api/messaging/messages")
async def list_messages(
    appointment_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Message)
    if appointment_id:
        stmt = stmt.where(Message.appointment_id == uuid.UUID(appointment_id))
    stmt = stmt.order_by(Message.created_at.desc()).limit(100)
    r = await db.execute(stmt)
    msgs = r.scalars().all()
    return [
        {"id": str(m.id), "subject": m.subject, "status": m.status,
         "channel": m.channel, "sent_at": m.sent_at, "created_at": m.created_at}
        for m in msgs
    ]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "messaging", "mock_mode": EMAIL_MOCK_MODE}
