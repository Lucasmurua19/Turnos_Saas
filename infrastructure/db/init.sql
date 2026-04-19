-- ============================================================
-- Turnos SaaS — Esquema inicial
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── Enums ────────────────────────────────────────────────────────

CREATE TYPE tenant_status AS ENUM ('active', 'inactive', 'suspended');
CREATE TYPE tenant_plan AS ENUM ('basic', 'pro', 'enterprise');
CREATE TYPE user_role AS ENUM ('superadmin', 'admin_tenant', 'secretaria', 'medico');
CREATE TYPE appointment_status AS ENUM (
  'programado',
  'pendiente_confirmacion',
  'confirmado',
  'cancelado_paciente',
  'cancelado_consultorio',
  'sin_respuesta',
  'reprogramado',
  'completado',
  'ausente'
);
CREATE TYPE message_status AS ENUM ('pending', 'sent', 'delivered', 'failed', 'replied');
CREATE TYPE channel_type AS ENUM ('email', 'whatsapp', 'sms');

-- ─── Tenants ──────────────────────────────────────────────────────

CREATE TABLE tenants (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  slug          VARCHAR(100) UNIQUE NOT NULL,
  name          VARCHAR(255) NOT NULL,
  email         VARCHAR(255) NOT NULL,
  phone         VARCHAR(50),
  plan          tenant_plan NOT NULL DEFAULT 'basic',
  status        tenant_status NOT NULL DEFAULT 'active',
  settings      JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Usuarios ─────────────────────────────────────────────────────

CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email         VARCHAR(255) NOT NULL,
  hashed_password VARCHAR(255) NOT NULL,
  full_name     VARCHAR(255) NOT NULL,
  role          user_role NOT NULL DEFAULT 'secretaria',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, email)
);

-- ─── Sedes ────────────────────────────────────────────────────────

CREATE TABLE locations (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          VARCHAR(255) NOT NULL,
  address       TEXT,
  phone         VARCHAR(50),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Profesionales ────────────────────────────────────────────────

CREATE TABLE professionals (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id       UUID REFERENCES users(id),
  location_id   UUID REFERENCES locations(id),
  full_name     VARCHAR(255) NOT NULL,
  specialty     VARCHAR(255),
  email         VARCHAR(255),
  phone         VARCHAR(50),
  license_number VARCHAR(100),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Pacientes ────────────────────────────────────────────────────

CREATE TABLE patients (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  full_name     VARCHAR(255) NOT NULL,
  email         VARCHAR(255),
  phone         VARCHAR(50),
  dni           VARCHAR(20),
  date_of_birth DATE,
  preferred_channel channel_type DEFAULT 'email',
  notes         TEXT,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Agendas (disponibilidad semanal) ─────────────────────────────

CREATE TABLE schedules (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  professional_id UUID NOT NULL REFERENCES professionals(id) ON DELETE CASCADE,
  location_id     UUID NOT NULL REFERENCES locations(id),
  day_of_week     SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  start_time      TIME NOT NULL,
  end_time        TIME NOT NULL,
  slot_duration   SMALLINT NOT NULL DEFAULT 30,  -- minutos
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Turnos ───────────────────────────────────────────────────────

CREATE TABLE appointments (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  professional_id UUID NOT NULL REFERENCES professionals(id),
  patient_id      UUID NOT NULL REFERENCES patients(id),
  location_id     UUID NOT NULL REFERENCES locations(id),
  scheduled_at    TIMESTAMPTZ NOT NULL,
  duration        SMALLINT NOT NULL DEFAULT 30,
  status          appointment_status NOT NULL DEFAULT 'programado',
  notes           TEXT,
  created_by      UUID REFERENCES users(id),
  rescheduled_from UUID REFERENCES appointments(id),
  reminder_sent_at TIMESTAMPTZ,
  confirmed_at    TIMESTAMPTZ,
  cancelled_at    TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Mensajes ─────────────────────────────────────────────────────

CREATE TABLE messages (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  appointment_id  UUID REFERENCES appointments(id),
  patient_id      UUID NOT NULL REFERENCES patients(id),
  channel         channel_type NOT NULL DEFAULT 'email',
  template_key    VARCHAR(100),
  subject         VARCHAR(255),
  body            TEXT NOT NULL,
  status          message_status NOT NULL DEFAULT 'pending',
  external_id     VARCHAR(255),
  sent_at         TIMESTAMPTZ,
  delivered_at    TIMESTAMPTZ,
  reply_received  TEXT,
  reply_at        TIMESTAMPTZ,
  error_detail    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Índices ──────────────────────────────────────────────────────

CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_locations_tenant ON locations(tenant_id);
CREATE INDEX idx_professionals_tenant ON professionals(tenant_id);
CREATE INDEX idx_patients_tenant ON patients(tenant_id);
CREATE INDEX idx_schedules_professional ON schedules(professional_id);
CREATE INDEX idx_appointments_tenant ON appointments(tenant_id);
CREATE INDEX idx_appointments_professional ON appointments(professional_id, scheduled_at);
CREATE INDEX idx_appointments_patient ON appointments(patient_id);
CREATE INDEX idx_appointments_status ON appointments(tenant_id, status);
CREATE INDEX idx_appointments_scheduled ON appointments(scheduled_at) WHERE status NOT IN ('cancelado_paciente','cancelado_consultorio','completado','ausente');
CREATE INDEX idx_messages_appointment ON messages(appointment_id);
CREATE INDEX idx_messages_status ON messages(status);

-- ─── Trigger updated_at ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenants','users','locations','professionals','patients','schedules','appointments','messages']
  LOOP
    EXECUTE format('CREATE TRIGGER trg_%I_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at()', t, t);
  END LOOP;
END $$;

-- ─── Superadmin y tenant demo ─────────────────────────────────────

INSERT INTO tenants (id, slug, name, email, plan, status) VALUES
  ('00000000-0000-0000-0000-000000000001', 'demo', 'Consultorio Demo', 'demo@turnossaas.com', 'pro', 'active');

-- password: admin123 (bcrypt)
INSERT INTO users (tenant_id, email, hashed_password, full_name, role) VALUES
  ('00000000-0000-0000-0000-000000000001',
   'admin@demo.com',
   '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW',
   'Admin Demo',
   'admin_tenant');
