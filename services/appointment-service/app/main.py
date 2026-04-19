"""
Appointment Service — Núcleo del negocio.
Gestiona pacientes, profesionales, sedes, agendas y turnos.
Incluye máquina de estados y validación de superposición.
"""
import uuid, os, logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional, List
from enum import Enum

from fastapi import FastAPI, HTTPException, Depends, Header, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import Column, String, Boolean, Integer, SmallInteger, Text, Date, Time, ForeignKey, text, Enum as SqlEnum, Interval
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.future import select
from sqlalchemy import and_, func

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("appointment-service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://turnos:turnos_secret@postgres:5432/turnos_db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ─── ORM ──────────────────────────────────────────────────────────

class Base(DeclarativeBase): pass

class Location(Base):
    __tablename__ = "locations"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id  = Column(UUID(as_uuid=True), nullable=False)
    name       = Column(String(255), nullable=False)
    address    = Column(Text)
    phone      = Column(String(50))
    is_active  = Column(Boolean, default=True)

class Professional(Base):
    __tablename__ = "professionals"
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), nullable=False)
    location_id    = Column(UUID(as_uuid=True), ForeignKey("locations.id"))
    full_name      = Column(String(255), nullable=False)
    specialty      = Column(String(255))
    email          = Column(String(255))
    phone          = Column(String(50))
    license_number = Column(String(100))
    is_active      = Column(Boolean, default=True)

class ChannelType(str, Enum):
    email = "email"
    whatsapp = "whatsapp"
    sms = "sms"

class Patient(Base):
    __tablename__ = "patients"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id     = Column(UUID(as_uuid=True), nullable=False)
    full_name     = Column(String(255), nullable=False)
    email         = Column(String(255))
    phone         = Column(String(50))
    dni           = Column(String(20))
    date_of_birth = Column(Date)
    preferred_channel = Column(SqlEnum(ChannelType, name='channel_type'), default=ChannelType.email)
    notes         = Column(Text)
    is_active     = Column(Boolean, default=True)

class Schedule(Base):
    __tablename__ = "schedules"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    professional_id = Column(UUID(as_uuid=True), ForeignKey("professionals.id"))
    location_id     = Column(UUID(as_uuid=True), ForeignKey("locations.id"))
    day_of_week     = Column(SmallInteger, nullable=False)
    start_time      = Column(Time, nullable=False)
    end_time        = Column(Time, nullable=False)
    slot_duration   = Column(SmallInteger, default=30)
    is_active       = Column(Boolean, default=True)

# ─── Máquina de estados ───────────────────────────────────────────

class AppointmentStatus(str, Enum):
    programado             = "programado"
    pendiente_confirmacion = "pendiente_confirmacion"
    confirmado             = "confirmado"
    cancelado_paciente     = "cancelado_paciente"
    cancelado_consultorio  = "cancelado_consultorio"
    sin_respuesta          = "sin_respuesta"
    reprogramado           = "reprogramado"
    completado             = "completado"
    ausente                = "ausente"

class Appointment(Base):
    __tablename__ = "appointments"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    professional_id = Column(UUID(as_uuid=True), ForeignKey("professionals.id"))
    patient_id      = Column(UUID(as_uuid=True), ForeignKey("patients.id"))
    location_id     = Column(UUID(as_uuid=True), ForeignKey("locations.id"))
    scheduled_at    = Column(TIMESTAMP(timezone=True), nullable=False)
    duration        = Column(SmallInteger, default=30)
    status          = Column(SqlEnum(AppointmentStatus, name='appointment_status'), default=AppointmentStatus.programado)
    notes           = Column(Text)
    created_by      = Column(UUID(as_uuid=True))
    rescheduled_from= Column(UUID(as_uuid=True))
    reminder_sent_at= Column(TIMESTAMP(timezone=True))
    confirmed_at    = Column(TIMESTAMP(timezone=True))
    cancelled_at    = Column(TIMESTAMP(timezone=True))
    completed_at    = Column(TIMESTAMP(timezone=True))
    created_at      = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))
    updated_at      = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))

VALID_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.programado: {
        AppointmentStatus.pendiente_confirmacion,
        AppointmentStatus.confirmado,
        AppointmentStatus.cancelado_consultorio,
        AppointmentStatus.cancelado_paciente,
        AppointmentStatus.reprogramado,
    },
    AppointmentStatus.pendiente_confirmacion: {
        AppointmentStatus.confirmado,
        AppointmentStatus.cancelado_paciente,
        AppointmentStatus.cancelado_consultorio,
        AppointmentStatus.sin_respuesta,
    },
    AppointmentStatus.confirmado: {
        AppointmentStatus.completado,
        AppointmentStatus.ausente,
        AppointmentStatus.cancelado_paciente,
        AppointmentStatus.cancelado_consultorio,
        AppointmentStatus.reprogramado,
    },
    AppointmentStatus.sin_respuesta: {
        AppointmentStatus.cancelado_consultorio,
        AppointmentStatus.confirmado,
    },
    AppointmentStatus.reprogramado: set(),
    AppointmentStatus.completado: set(),
    AppointmentStatus.ausente: set(),
    AppointmentStatus.cancelado_paciente: {
        AppointmentStatus.reprogramado,
        AppointmentStatus.confirmado,
        AppointmentStatus.pendiente_confirmacion,
    },
    AppointmentStatus.cancelado_consultorio: {
        AppointmentStatus.reprogramado,
        AppointmentStatus.confirmado,
        AppointmentStatus.pendiente_confirmacion,
    },
}

def can_transition(current: str, target: str) -> bool:
    try:
        c = AppointmentStatus(current)
        t = AppointmentStatus(target)
        return t in VALID_TRANSITIONS.get(c, set())
    except ValueError:
        return False

# ─── Schemas ──────────────────────────────────────────────────────

class LocationCreate(BaseModel):
    name: str; address: Optional[str]=None; phone: Optional[str]=None

class LocationResponse(BaseModel):
    id: uuid.UUID; tenant_id: uuid.UUID; name: str
    address: Optional[str]=None; phone: Optional[str]=None; is_active: bool
    class Config: from_attributes=True

class ProfessionalCreate(BaseModel):
    location_id: Optional[uuid.UUID]=None; full_name: str
    specialty: Optional[str]=None; email: Optional[str]=None
    phone: Optional[str]=None; license_number: Optional[str]=None

class ProfessionalResponse(BaseModel):
    id: uuid.UUID; tenant_id: uuid.UUID; full_name: str
    specialty: Optional[str]=None; email: Optional[str]=None
    phone: Optional[str]=None; location_id: Optional[uuid.UUID]=None
    is_active: bool
    class Config: from_attributes=True

class PatientCreate(BaseModel):
    full_name: str; email: Optional[str]=None; phone: Optional[str]=None
    dni: Optional[str]=None; date_of_birth: Optional[date]=None
    preferred_channel: str="email"; notes: Optional[str]=None

class PatientResponse(BaseModel):
    id: uuid.UUID; tenant_id: uuid.UUID; full_name: str
    email: Optional[str]=None; phone: Optional[str]=None
    dni: Optional[str]=None; preferred_channel: str; is_active: bool
    class Config: from_attributes=True

class ScheduleCreate(BaseModel):
    professional_id: uuid.UUID; location_id: uuid.UUID
    day_of_week: int; start_time: time; end_time: time; slot_duration: int=30

class ScheduleResponse(BaseModel):
    id: uuid.UUID; professional_id: uuid.UUID; day_of_week: int
    start_time: time; end_time: time; slot_duration: int; is_active: bool
    class Config: from_attributes=True

class AppointmentCreate(BaseModel):
    professional_id: uuid.UUID; patient_id: uuid.UUID
    location_id: uuid.UUID; scheduled_at: datetime
    duration: int=30; notes: Optional[str]=None

class AppointmentUpdate(BaseModel):
    scheduled_at: Optional[datetime]=None
    status: Optional[str]=None; notes: Optional[str]=None

class AppointmentResponse(BaseModel):
    id: uuid.UUID; tenant_id: uuid.UUID; professional_id: uuid.UUID
    patient_id: uuid.UUID; location_id: uuid.UUID
    scheduled_at: datetime; duration: int; status: str
    notes: Optional[str]=None; created_at: datetime; updated_at: datetime
    reminder_sent_at: Optional[datetime]=None
    confirmed_at: Optional[datetime]=None
    class Config: from_attributes=True

# ─── Helpers ──────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def get_tenant_id(x_tenant_id: str = Header(...)) -> uuid.UUID:
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Tenant-ID inválido")

async def check_no_overlap(
    db: AsyncSession, professional_id: uuid.UUID,
    scheduled_at: datetime, duration: int, exclude_id: Optional[uuid.UUID]=None
):
    end_at = scheduled_at + timedelta(minutes=duration)
    q = select(Appointment).where(
        and_(
            Appointment.professional_id == professional_id,
            Appointment.status.not_in([
                "cancelado_paciente","cancelado_consultorio","reprogramado"
            ]),
            Appointment.scheduled_at < end_at,
            Appointment.scheduled_at + func.cast(
                func.concat(Appointment.duration, ' minutes'), Interval
            ) > scheduled_at,
        )
    )
    if exclude_id:
        q = q.where(Appointment.id != exclude_id)
    result = await db.execute(q)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="El profesional ya tiene un turno en ese horario"
        )

# ─── App ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Appointment Service iniciado")
    yield

app = FastAPI(title="Appointment Service", version="1.0.0", lifespan=lifespan)

# ─── LOCATIONS ────────────────────────────────────────────────────

@app.post("/api/appointments/locations", response_model=LocationResponse, status_code=201)
async def create_location(body: LocationCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    loc = Location(id=uuid.uuid4(), tenant_id=tenant_id, **body.model_dump())
    db.add(loc); await db.commit(); await db.refresh(loc)
    return loc

@app.get("/api/appointments/locations", response_model=List[LocationResponse])
async def list_locations(tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Location).where(Location.tenant_id==tenant_id, Location.is_active==True))
    return r.scalars().all()

@app.put("/api/appointments/locations/{loc_id}", response_model=LocationResponse)
async def update_location(loc_id: uuid.UUID, body: LocationCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Location).where(Location.id==loc_id, Location.tenant_id==tenant_id))
    loc = r.scalar_one_or_none()
    if not loc: raise HTTPException(404, "Sede no encontrada")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(loc, k, v)
    await db.commit(); await db.refresh(loc)
    return loc

@app.patch("/api/appointments/locations/{loc_id}/toggle")
async def toggle_location(loc_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Location).where(Location.id==loc_id, Location.tenant_id==tenant_id))
    loc = r.scalar_one_or_none()
    if not loc: raise HTTPException(404, "Sede no encontrada")
    loc.is_active = not loc.is_active
    await db.commit()
    return {"id": str(loc_id), "is_active": loc.is_active}

# ─── PROFESSIONALS ────────────────────────────────────────────────

@app.post("/api/appointments/professionals", response_model=ProfessionalResponse, status_code=201)
async def create_professional(body: ProfessionalCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    prof = Professional(id=uuid.uuid4(), tenant_id=tenant_id, **body.model_dump())
    db.add(prof); await db.commit(); await db.refresh(prof)
    return prof

@app.get("/api/appointments/professionals", response_model=List[ProfessionalResponse])
async def list_professionals(tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Professional).where(Professional.tenant_id==tenant_id, Professional.is_active==True))
    return r.scalars().all()

@app.get("/api/appointments/professionals/{prof_id}", response_model=ProfessionalResponse)
async def get_professional(prof_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Professional).where(Professional.id==prof_id, Professional.tenant_id==tenant_id))
    prof = r.scalar_one_or_none()
    if not prof: raise HTTPException(404, "Profesional no encontrado")
    return prof

@app.patch("/api/appointments/professionals/{prof_id}/toggle")
async def toggle_professional(prof_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Professional).where(Professional.id==prof_id, Professional.tenant_id==tenant_id))
    prof = r.scalar_one_or_none()
    if not prof: raise HTTPException(404, "Profesional no encontrado")
    prof.is_active = not prof.is_active
    await db.commit()
    return {"id": str(prof_id), "is_active": prof.is_active}

@app.put("/api/appointments/professionals/{prof_id}", response_model=ProfessionalResponse)
async def update_professional(prof_id: uuid.UUID, body: ProfessionalCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Professional).where(Professional.id==prof_id, Professional.tenant_id==tenant_id))
    prof = r.scalar_one_or_none()
    if not prof: raise HTTPException(404, "Profesional no encontrado")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(prof, k, v)
    await db.commit(); await db.refresh(prof)
    return prof

# ─── PATIENTS ─────────────────────────────────────────────────────

@app.post("/api/appointments/patients", response_model=PatientResponse, status_code=201)
async def create_patient(body: PatientCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    patient = Patient(id=uuid.uuid4(), tenant_id=tenant_id, **body.model_dump())
    db.add(patient); await db.commit(); await db.refresh(patient)
    return patient

@app.get("/api/appointments/patients", response_model=List[PatientResponse])
async def list_patients(
    q: Optional[str]=Query(None, description="Buscar por nombre o teléfono"),
    tenant_id: uuid.UUID=Depends(get_tenant_id),
    db: AsyncSession=Depends(get_db)
):
    stmt = select(Patient).where(Patient.tenant_id==tenant_id, Patient.is_active==True)
    if q:
        stmt = stmt.where(
            Patient.full_name.ilike(f"%{q}%") | Patient.phone.ilike(f"%{q}%") | Patient.email.ilike(f"%{q}%")
        )
    r = await db.execute(stmt)
    return r.scalars().all()

@app.get("/api/appointments/patients/{patient_id}", response_model=PatientResponse)
async def get_patient(patient_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Patient).where(Patient.id==patient_id, Patient.tenant_id==tenant_id))
    patient = r.scalar_one_or_none()
    if not patient: raise HTTPException(404, "Paciente no encontrado")
    return patient

@app.put("/api/appointments/patients/{patient_id}", response_model=PatientResponse)
async def update_patient(patient_id: uuid.UUID, body: PatientCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Patient).where(Patient.id==patient_id, Patient.tenant_id==tenant_id))
    patient = r.scalar_one_or_none()
    if not patient: raise HTTPException(404, "Paciente no encontrado")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(patient, k, v)
    await db.commit(); await db.refresh(patient)
    return patient

@app.patch("/api/appointments/patients/{patient_id}/toggle")
async def toggle_patient(patient_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Patient).where(Patient.id==patient_id, Patient.tenant_id==tenant_id))
    patient = r.scalar_one_or_none()
    if not patient: raise HTTPException(404, "Paciente no encontrado")
    patient.is_active = not patient.is_active
    await db.commit()
    return {"id": str(patient_id), "is_active": patient.is_active}

# ─── SCHEDULES ────────────────────────────────────────────────────

@app.post("/api/appointments/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule(body: ScheduleCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    sched = Schedule(id=uuid.uuid4(), tenant_id=tenant_id, **body.model_dump())
    db.add(sched); await db.commit(); await db.refresh(sched)
    return sched

@app.get("/api/appointments/schedules", response_model=List[ScheduleResponse])
async def list_schedules(
    professional_id: Optional[uuid.UUID]=Query(None),
    tenant_id: uuid.UUID=Depends(get_tenant_id),
    db: AsyncSession=Depends(get_db)
):
    stmt = select(Schedule).where(Schedule.tenant_id==tenant_id, Schedule.is_active==True)
    if professional_id: stmt = stmt.where(Schedule.professional_id==professional_id)
    r = await db.execute(stmt)
    return r.scalars().all()

# ─── APPOINTMENTS ─────────────────────────────────────────────────

@app.post("/api/appointments", response_model=AppointmentResponse, status_code=201)
async def create_appointment(body: AppointmentCreate, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    await check_no_overlap(db, body.professional_id, body.scheduled_at, body.duration)
    appt = Appointment(id=uuid.uuid4(), tenant_id=tenant_id, **body.model_dump())
    db.add(appt); await db.commit(); await db.refresh(appt)
    logger.info("Turno creado: %s", appt.id)
    return appt

@app.get("/api/appointments", response_model=List[AppointmentResponse])
async def list_appointments(
    date: Optional[str]=Query(None),
    professional_id: Optional[uuid.UUID]=Query(None),
    patient_id: Optional[uuid.UUID]=Query(None),
    status_filter: Optional[str]=Query(None, alias="status"),
    tenant_id: uuid.UUID=Depends(get_tenant_id),
    db: AsyncSession=Depends(get_db)
):
    stmt = select(Appointment).where(Appointment.tenant_id==tenant_id)
    if date:
        day = datetime.strptime(date, "%Y-%m-%d")
        stmt = stmt.where(
            Appointment.scheduled_at >= day,
            Appointment.scheduled_at < day + timedelta(days=1)
        )
    if professional_id: stmt = stmt.where(Appointment.professional_id==professional_id)
    if patient_id:      stmt = stmt.where(Appointment.patient_id==patient_id)
    if status_filter:   stmt = stmt.where(Appointment.status==status_filter)
    stmt = stmt.order_by(Appointment.scheduled_at)
    r = await db.execute(stmt)
    return r.scalars().all()

@app.get("/api/appointments/{appt_id}", response_model=AppointmentResponse)
async def get_appointment(appt_id: uuid.UUID, tenant_id: uuid.UUID=Depends(get_tenant_id), db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Appointment).where(Appointment.id==appt_id, Appointment.tenant_id==tenant_id))
    appt = r.scalar_one_or_none()
    if not appt: raise HTTPException(404, "Turno no encontrado")
    return appt

@app.patch("/api/appointments/{appt_id}", response_model=AppointmentResponse)
async def update_appointment(
    appt_id: uuid.UUID, body: AppointmentUpdate,
    tenant_id: uuid.UUID=Depends(get_tenant_id),
    db: AsyncSession=Depends(get_db)
):
    r = await db.execute(select(Appointment).where(Appointment.id==appt_id, Appointment.tenant_id==tenant_id))
    appt = r.scalar_one_or_none()
    if not appt: raise HTTPException(404, "Turno no encontrado")

    if body.status and body.status != appt.status:
        if not can_transition(appt.status, body.status):
            raise HTTPException(
                status_code=422,
                detail=f"Transición inválida: {appt.status} → {body.status}"
            )
        # Timestamps automáticos por estado
        now = datetime.now(timezone.utc)
        if body.status == "confirmado":             appt.confirmed_at = now
        elif body.status in ("cancelado_paciente","cancelado_consultorio"): appt.cancelled_at = now
        elif body.status == "completado":           appt.completed_at = now
        appt.status = body.status

    if body.scheduled_at:
        await check_no_overlap(db, appt.professional_id, body.scheduled_at, appt.duration, exclude_id=appt_id)
        appt.scheduled_at = body.scheduled_at

    if body.notes is not None: appt.notes = body.notes
    appt.updated_at = datetime.now(timezone.utc)
    await db.commit(); await db.refresh(appt)
    return appt

@app.delete("/api/appointments/{appt_id}", status_code=204)
async def cancel_appointment(
    appt_id: uuid.UUID, by: str=Query("consultorio"),
    tenant_id: uuid.UUID=Depends(get_tenant_id),
    db: AsyncSession=Depends(get_db)
):
    r = await db.execute(select(Appointment).where(Appointment.id==appt_id, Appointment.tenant_id==tenant_id))
    appt = r.scalar_one_or_none()
    if not appt: raise HTTPException(404, "Turno no encontrado")
    target_status = "cancelado_paciente" if by == "paciente" else "cancelado_consultorio"
    if not can_transition(appt.status, target_status):
        raise HTTPException(422, f"No se puede cancelar un turno en estado: {appt.status}")
    appt.status = target_status
    appt.cancelled_at = datetime.now(timezone.utc)
    await db.commit()

# ─── Endpoint interno: turnos a recordar (para notification-service) ─

@app.get("/api/appointments/internal/pending-reminders")
async def pending_reminders(hours_ahead: int=Query(24), db: AsyncSession=Depends(get_db)):
    """Devuelve turnos que necesitan recordatorio en las próximas N horas."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=hours_ahead)
    r = await db.execute(
        select(Appointment).where(
            and_(
                Appointment.status == "programado",
                Appointment.scheduled_at > now,
                Appointment.scheduled_at <= window_end,
                Appointment.reminder_sent_at == None,
            )
        )
    )
    return [
        {"id": str(a.id), "tenant_id": str(a.tenant_id),
         "patient_id": str(a.patient_id), "scheduled_at": a.scheduled_at.isoformat(),
         "professional_id": str(a.professional_id)}
        for a in r.scalars().all()
    ]

@app.post("/api/appointments/{appt_id}/mark-reminder-sent")
async def mark_reminder_sent(appt_id: uuid.UUID, db: AsyncSession=Depends(get_db)):
    r = await db.execute(select(Appointment).where(Appointment.id==appt_id))
    appt = r.scalar_one_or_none()
    if appt:
        appt.reminder_sent_at = datetime.now(timezone.utc)
        appt.status = "pendiente_confirmacion"
        await db.commit()
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "appointments"}
