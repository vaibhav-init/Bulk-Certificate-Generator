# Bulk Certificate Generator

FastAPI + PostgreSQL backend that accepts one request with many recipients, renders a PDF certificate per valid recipient from a single fixed template, tracks per-job and per-certificate status, and serves the PDFs back.

## Stack

- **FastAPI** — API framework (required)
- **PostgreSQL** — relational store (required)
- **psycopg 3** (raw SQL, no ORM) — smaller surface than SQLAlchemy for 2 tables
- **reportlab** — PDF rendering
- **BackgroundTasks + ThreadPoolExecutor** — bulk generation

## Setup

Prereqs: Python 3.12+ and a reachable PostgreSQL (a container works fine).

```bash
# 1. Postgres (any instance you have; this one matches the defaults below)
docker run -d --name certgen-pg -e POSTGRES_USER=certuser -e POSTGRES_PASSWORD=certpass \
  -e POSTGRES_DB=certdb -p 5433:5432 postgres:16

# 2. Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --reload
# or: python -m uvicorn main:app --reload
```

Environment variables (all optional):

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://certuser:certpass@localhost:5433/certdb` | Postgres DSN |
| `CERT_DIR` | `./certificates` | Where PDFs are written |
| `CERT_WORKERS` | `8` | Threads used per job |

## Run tests

Same Postgres instance is reused; tests write to a separate `CERT_DIR`.

```bash
pytest -v
```

## API

### Submit a generation request

`POST /certificates/generate` → `202 Accepted`

```bash
curl -s -X POST localhost:8000/certificates/generate \
  -H 'Content-Type: application/json' -d '{
    "course": "Intro to Alchemy",
    "recipients": [
      {"name": "Ada Lovelace", "email": "ada@example.com"},
      {"name": "Grace Hopper",  "email": "grace@example.com"}
    ]
  }'
# → {"job_id": "...", "status": "pending", "total": 2}
```

Request validation: `course` 1–200 chars, each recipient needs a non-empty name (≤200) and a valid email; duplicate emails within one request (case-insensitive) are rejected with 422. Empty recipient lists are rejected. One invalid recipient fails the whole request (fail fast) — per-certificate runtime failures are handled separately (see Design decisions).

### Check job status

`GET /certificates/jobs/{job_id}`

```bash
curl -s localhost:8000/certificates/jobs/<job_id>
# → {"job_id":"...","status":"completed","total":2,"completed":2,"failed":0,"certificates":2}
```

### List a job's certificates

`GET /certificates/jobs/{job_id}/certificates`

```bash
curl -s localhost:8000/certificates/jobs/<job_id>/certificates
# → per-certificate records: recipient, email, status ("success"/"failed"),
#   error (why it failed), download_url (null for failed certs)
```

### Download a certificate

`GET /certificates/{certificate_id}` → the PDF (`application/pdf`), or `410` if the DB row exists but the file is gone.

```bash
curl -s -o ada.pdf localhost:8000/certificates/<certificate_id>
```

## Design decisions

**Background processing, not synchronous.** A 500-recipient request is a batch job: keeping the HTTP connection open while rendering 500 PDFs risks client timeouts and gives the client nothing to poll. Instead `POST /certificates/generate` persists the job, kicks off generation via FastAPI's `BackgroundTasks`, and returns `202 Accepted` immediately with a `job_id`. Status is tracked in Postgres, so progress survives an HTTP round trip. Trade-off: BackgroundTasks runs in the API process (no persistence across restarts), which is the right size for this assignment; a real deployment would swap it for a task queue (Celery + broker) without touching the API contract, since status and results live in the DB.

**Failure isolation.** `run_job` renders every recipient in a thread pool; each PDF render is wrapped so one failure records `status='failed'` + the error string for that recipient only. The job still completes, and the per-job counters (`completed`/`failed`) let the client identify exactly which recipients succeeded and why the others didn't, via the listing endpoint.

**Status model.** Job: `pending → completed` (or `failed` only if results can't be persisted at all, e.g. DB down after rendering). Certificate: `success` / `failed`. A `failed` job means "results not recorded", not "some certs failed" — per-certificate failures are visible on the job as `failed > 0` with status `completed`.

**Template.** One fixed reportlab template: bordered A4 page, "Certificate of Completion", recipient name and course drawn centred; the certificate UUID is printed on the PDF and is also the download handle. No template editor, as allowed.

**No ORM.** Two tables and five queries; psycopg row tuples + plain SQL keep the code shorter than a mapper layer. Schema is created idempotently at startup (`CREATE TABLE IF NOT EXISTS`).

**Validation split.** Request-shape validation (missing fields, bad email, duplicates, empty list) happens synchronously at submit time — rejecting a whole malformed batch is correct and cheap. Per-certificate *runtime* failures (disk error, etc.) happen during generation and are recorded per certificate, so one bad recipient never blocks 499 good ones.

**Progress tracking.** Results are persisted and job counters updated in 100-recipient chunks as generation proceeds, so a client polling the status endpoint sees `completed`/`failed` climb while a large job is still running (job stays `pending` until the final chunk lands, then flips to `completed`). Per-certificate rows appear in the listing incrementally for the same reason.

**Concurrency.** PDF rendering is CPU-bound-ish and file-I/O-bound, so a thread pool (default 8, tunable) parallelises within a job; Postgres connections are opened per operation, no pool needed at this scale.

## Schema

```
jobs(id, status, course, total, completed, failed, created_at)
certificates(id, job_id → jobs.id, recipient, email, status, error, file_path, created_at)
```
