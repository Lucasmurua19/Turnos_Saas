"""
Notification Service — Detecta turnos próximos, dispara recordatorios
y procesa respuestas de confirmación/cancelación vía email.
Usa APScheduler para el cron dentro del mismo proceso.
"""
import uuid, os, logging, httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from sqlalchemy import Column, String, Text, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.future import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-service")

DATABASE_URL         = os.getenv("DATABASE_URL","postgresql+asyncpg://turnos:turnos_secret@postgres:5432/turnos_db")
APPOINTMENT_SVC_URL  = os.getenv("APPOINTMENT_SERVICE_URL","http://appointment-service:8003")
MESSAGING_SVC_URL    = os.getenv("MESSAGING_SERVICE_URL","http://messaging-service:8005")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ─── ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase): pass

class Patient(Base):
    __tablename__ = "patients"
    id                = Column(UUID(as_uuid=True), primary_key=True)
    tenant_id         = Column(UUID(as_uuid=True))
    full_name         = Column(String(255))
    email             = Column(String(255))
    phone             = Column(String(50))
    preferred_channel = Column(String(20), default="email")

class Professional(Base):
    __tablename__ = "professionals"
    id        = Column(UUID(as_uuid=True), primary_key=True)
    full_name = Column(String(255))
    specialty = Column(String(255))

# ─── Helpers ──────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# ─── Tarea del scheduler ──────────────────────────────────────────

async def run_reminder_check():
    """
    Cron que se ejecuta cada hora:
    1. Pide al appointment-service los turnos en las próximas 24hs sin recordatorio.
    2. Para cada uno, busca datos del paciente y envía el email de recordatorio.
    3. Marca el turno como reminder_sent y cambia estado a pendiente_confirmacion.
    """
    logger.info("[Cron] Buscando turnos para recordar...")
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                f"{APPOINTMENT_SVC_URL}/api/appointments/internal/pending-reminders",
                params={"hours_ahead": 24}
            )
            r.raise_for_status()
            appointments = r.json()
        except Exception as e:
            logger.error("[Cron] Error consultando turnos: %s", e)
            return

        logger.info("[Cron] %d turnos pendientes de recordatorio", len(appointments))

        async with AsyncSessionLocal() as db:
            for appt in appointments:
                try:
                    patient_r = await db.execute(
                        select(Patient).where(Patient.id == uuid.UUID(appt["patient_id"]))
                    )
                    patient = patient_r.scalar_one_or_none()
                    if not patient or not patient.email:
                        logger.warning("[Cron] Paciente sin email: %s", appt["patient_id"])
                        continue

                    prof_r = await db.execute(
                        select(Professional).where(Professional.id == uuid.UUID(appt["professional_id"]))
                    )
                    professional = prof_r.scalar_one_or_none()
                    prof_name = professional.full_name if professional else "su médico"

                    scheduled_dt = datetime.fromisoformat(appt["scheduled_at"])
                    fecha = scheduled_dt.strftime("%d/%m/%Y a las %H:%M")

                    # Enviar al messaging-service
                    msg_payload = {
                        "tenant_id": appt["tenant_id"],
                        "appointment_id": appt["id"],
                        "patient_id": appt["patient_id"],
                        "channel": "email",
                        "template_key": "reminder",
                        "to_email": patient.email,
                        "subject": f"Recordatorio de turno — {fecha}",
                        "variables": {
                            "patient_name": patient.full_name,
                            "professional_name": prof_name,
                            "date": fecha,
                            "appointment_id": appt["id"],
                        }
                    }
                    msg_r = await client.post(
                        f"{MESSAGING_SVC_URL}/api/messaging/send",
                        json=msg_payload
                    )
                    if msg_r.status_code in (200, 201):
                        # Marcar turno como recordatorio enviado
                        await client.post(
                            f"{APPOINTMENT_SVC_URL}/api/appointments/{appt['id']}/mark-reminder-sent"
                        )
                        logger.info("[Cron] Recordatorio enviado para turno %s", appt["id"])
                    else:
                        logger.warning("[Cron] Error enviando mensaje: %s", msg_r.text)

                except Exception as e:
                    logger.error("[Cron] Error procesando turno %s: %s", appt.get("id"), e)

# ─── Schemas ──────────────────────────────────────────────────────

class ReplyWebhook(BaseModel):
    appointment_id: str
    tenant_id: str
    patient_response: str  # "1" = confirmar, "2" = cancelar

# ─── App ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_reminder_check,
        trigger="interval",
        hours=1,
        id="reminder_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Notification Service iniciado. Scheduler activo (cada 1h)")
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)

app = FastAPI(title="Notification Service", version="1.0.0", lifespan=lifespan)

@app.post("/api/notifications/trigger-reminders")
async def trigger_reminders_manually():
    """Endpoint para disparar el cron manualmente (útil para tests)."""
    await run_reminder_check()
    return {"ok": True, "message": "Proceso de recordatorios ejecutado"}

@app.post("/api/notifications/process-reply")
async def process_reply(body: ReplyWebhook):
    """
    Procesa la respuesta de un paciente al recordatorio.
    "1" → confirmar, "2" → cancelar
    """
    new_status = None
    if body.patient_response.strip() == "1":
        new_status = "confirmado"
    elif body.patient_response.strip() == "2":
        new_status = "cancelado_paciente"
    else:
        logger.info("Respuesta no reconocida: %s", body.patient_response)
        return {"ok": False, "message": "Respuesta no reconocida"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.patch(
            f"{APPOINTMENT_SVC_URL}/api/appointments/{body.appointment_id}",
            json={"status": new_status},
            headers={"X-Tenant-ID": body.tenant_id},
        )
        if r.status_code not in (200, 201):
            logger.error("Error actualizando turno: %s", r.text)
            raise HTTPException(500, "No se pudo actualizar el turno")

    logger.info("Turno %s → %s (respuesta del paciente)", body.appointment_id, new_status)
    return {"ok": True, "new_status": new_status}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "notifications"}
