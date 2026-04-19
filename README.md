# Turnos SaaS

Sistema multi-tenant de gestión y automatización de turnos para consultorios médicos.

## Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│  Panel Web (puerto 3000)                                │
└───────────────────┬─────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────┐
│  API Gateway (puerto 8000)                              │
│  JWT validation · Tenant resolution · Proxy             │
└───┬───────────┬───────────┬──────────────┬──────────────┘
    │           │           │              │
┌───▼──┐  ┌────▼────┐ ┌────▼─────┐ ┌─────▼──────┐ ┌──────────────┐
│ Auth │  │ Tenant  │ │Appoint.  │ │Notif.      │ │ Messaging    │
│ :8001│  │  :8002  │ │  :8003   │ │ :8004      │ │   :8005      │
│      │  │         │ │ (core)   │ │ APScheduler│ │ Email/Mock   │
└──────┘  └─────────┘ └────┬─────┘ └─────┬──────┘ └──────────────┘
                            │             │
                     ┌──────▼─────────────▼──────┐
                     │  PostgreSQL + Redis        │
                     └───────────────────────────┘
```

## Microservicios

| Servicio | Puerto | Responsabilidad |
|----------|--------|-----------------|
| api-gateway | 8000 | Punto de entrada, JWT, proxy |
| auth-service | 8001 | Login, registro, tokens |
| tenant-service | 8002 | Alta y gestión de consultorios |
| appointment-service | 8003 | Turnos, pacientes, profesionales, sedes |
| notification-service | 8004 | Cron de recordatorios (APScheduler) |
| messaging-service | 8005 | Envío de emails (SendGrid/SMTP/Mock) |

## Inicio rápido

```bash
# 1. Clonar y configurar variables
cp .env.example .env

# 2. Levantar todo
docker compose up --build

# 3. Acceder
# Panel web:  http://localhost:3000
# API docs:   http://localhost:8000/docs
# Health:     http://localhost:8000/health

# Credenciales demo
# Email:    admin@demo.com
# Password: admin123
```

## Estados de turno

```
programado → pendiente_confirmacion → confirmado → completado
                                               └──→ ausente
           → cancelado_paciente
           → cancelado_consultorio
           → reprogramado
           → sin_respuesta → cancelado_consultorio
```

## Flujo de recordatorios

1. Cron corre cada hora en `notification-service`
2. Consulta turnos con `status=programado` en las próximas 24hs sin `reminder_sent_at`
3. Renderiza template y envía email vía `messaging-service`
4. Marca el turno como `pendiente_confirmacion` y registra `reminder_sent_at`
5. El paciente responde con `1` (confirmar) o `2` (cancelar)
6. El webhook `/api/notifications/process-reply` actualiza el estado

## Variables de entorno importantes

| Variable | Default | Descripción |
|----------|---------|-------------|
| `EMAIL_MOCK_MODE` | `true` | En `true` logea emails sin enviarlos |
| `SENDGRID_API_KEY` | — | API key de SendGrid para producción |
| `SECRET_KEY` | `change-me` | Clave JWT — **cambiar en producción** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Expiración del token JWT |

## Agregar un nuevo tenant

```bash
curl -X POST http://localhost:8002/api/tenants \
  -H "Content-Type: application/json" \
  -d '{"slug":"miclínica","name":"Mi Clínica","email":"admin@miclínica.com","plan":"pro"}'
```

## Estructura del proyecto

```
turnos-saas/
├── docker-compose.yml
├── .env.example
├── infrastructure/
│   └── db/
│       └── init.sql          ← Esquema completo
├── services/
│   ├── api-gateway/
│   ├── auth-service/
│   ├── tenant-service/
│   ├── appointment-service/  ← Núcleo del negocio
│   ├── notification-service/ ← APScheduler + lógica de recordatorios
│   └── messaging-service/    ← Email provider abstraction
└── frontend/
    └── index.html            ← Panel administrativo
```

## Roadmap post-MVP

- [ ] WhatsApp Business API integration
- [ ] Webhook de respuestas de email (SendGrid Inbound Parse)
- [ ] Panel de superadmin para gestión de tenants
- [ ] Migrar cron a Celery + Redis cuando el volumen lo requiera
- [ ] Slots de disponibilidad automáticos desde agenda semanal
- [ ] App móvil con React Native
