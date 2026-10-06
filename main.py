"""Bulk certificate generator.

One file on purpose: schema bootstrap, PDF renderer, API, and background
generation all fit comfortably here and nothing imports anything else.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field, field_validator

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://certuser:certpass@localhost:5433/certdb"
)
CERT_DIR = Path(os.environ.get("CERT_DIR", "certificates"))
# #ponytail: 8 workers is a guess, not a benchmark. Raise via CERT_WORKERS if
# a real deployment ever needs more; Postgres handles far more than this.
WORKERS = int(os.environ.get("CERT_WORKERS", "8"))

@asynccontextmanager
async def lifespan(_app):
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()  # defined below; first use is at startup, after module import completes
    yield


app = FastAPI(title="Bulk Certificate Generator", lifespan=lifespan)

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            UUID PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
    course        TEXT NOT NULL,
    total         INT NOT NULL CHECK (total > 0),
    completed     INT NOT NULL DEFAULT 0,
    failed        INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS certificates (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    recipient     TEXT NOT NULL,
    email         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error         TEXT,
    file_path     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_certs_job ON certificates (job_id);
"""


def get_conn():
    # #ponytail: connect_timeout so an unreachable DB fails in 5s instead of hanging forever
    return psycopg.connect(DATABASE_URL, connect_timeout=5)


def init_db():
    with get_conn() as conn:
        conn.execute(SCHEMA)


# ---------------------------------------------------------------- models

class Recipient(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr


class GenerationRequest(BaseModel):
    course: str = Field(min_length=1, max_length=200)
    recipients: list[Recipient] = Field(min_length=1, max_length=5000)

    @field_validator("recipients")
    @classmethod
    def unique_emails(cls, v):
        emails = [r.email.lower() for r in v]
        dupes = {e for e in emails if emails.count(e) > 1}
        if dupes:
            raise ValueError(f"duplicate recipient emails: {', '.join(sorted(dupes))}")
        return v


# ---------------------------------------------------------------- rendering

def render_pdf(path: Path, *, name: str, course: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.rect(15 * mm, 15 * mm, w - 30 * mm, h - 30 * mm)
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(w / 2, h - 70 * mm, "Certificate of Completion")
    c.setFont("Helvetica", 14)
    c.drawCentredString(w / 2, h - 90 * mm, "This certifies that")
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(w / 2, h - 105 * mm, name)
    c.setFont("Helvetica", 14)
    c.drawCentredString(w / 2, h - 120 * mm, "has successfully completed")
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 132 * mm, course)
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, 25 * mm, f"Certificate ID: {path.stem}")
    c.save()


# ---------------------------------------------------------------- generation

def generate_one(recipient: Recipient, course: str):
    """Generate one PDF; return (name, email, status, error, file_path)."""
    cert_id = uuid.uuid4()
    path = CERT_DIR / f"{cert_id}.pdf"
    try:
        render_pdf(path, name=recipient.name, course=course)
        return recipient.name, recipient.email, "success", None, str(path)
    except Exception as exc:  # noqa: BLE001 - one bad cert must not sink the job
        path.unlink(missing_ok=True)
        return recipient.name, recipient.email, "failed", str(exc), None


def run_job(job_id: uuid.UUID, req: GenerationRequest):
    # #ponytail: flush per 100-recipient chunk so a polling client sees the
    # completed/failed counters move during long jobs; per-cert flush would
    # double the DB round trips for no real gain at this scale.
    completed = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i in range(0, len(req.recipients), 100):
                results = list(
                    pool.map(lambda r: generate_one(r, req.course),
                             req.recipients[i:i + 100])
                )
                with get_conn() as conn:
                    conn.cursor().executemany(
                        """INSERT INTO certificates (job_id, recipient, email, status, error, file_path)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        [(job_id, *r) for r in results],
                    )
                    completed += sum(r[2] == "success" for r in results)
                    failed += sum(r[2] == "failed" for r in results)
                    conn.execute(
                        "UPDATE jobs SET completed = %s, failed = %s WHERE id = %s",
                        (completed, failed, job_id),
                    )
        with get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'completed', completed = %s, failed = %s WHERE id = %s",
                (completed, failed, job_id),
            )
    except Exception:  # noqa: BLE001
        # DB write failed after PDFs were generated: mark job failed so the
        # client sees an error instead of a job stuck in 'pending' forever.
        with get_conn() as conn:
            conn.execute("UPDATE jobs SET status = 'failed' WHERE id = %s", (job_id,))
        raise


# ---------------------------------------------------------------- endpoints

@app.post("/certificates/generate", status_code=202)
def create_job(req: GenerationRequest, background: BackgroundTasks):
    job_id = uuid.uuid4()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, course, total) VALUES (%s, 'pending', %s, %s)",
            (job_id, req.course, len(req.recipients)),
        )
    background.add_task(run_job, job_id, req)
    return {"job_id": str(job_id), "status": "pending", "total": len(req.recipients)}


@app.get("/certificates/jobs/{job_id}")
def job_status(job_id: uuid.UUID):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT j.id, j.status, j.total, j.completed, j.failed,
                      (SELECT count(*) FROM certificates c WHERE c.job_id = j.id) AS stored
               FROM jobs j WHERE j.id = %s""",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    id_, status, total, completed, failed, stored = row
    return {
        "job_id": str(id_),
        "status": status,
        "total": total,
        "completed": completed,
        "failed": failed,
        "certificates": stored,
    }


@app.get("/certificates/jobs/{job_id}/certificates")
def list_certificates(job_id: uuid.UUID):
    with get_conn() as conn:
        job = conn.execute("SELECT id FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(404, "job not found")
        rows = conn.execute(
            """SELECT id, recipient, email, status, error, file_path
               FROM certificates WHERE job_id = %s ORDER BY created_at""",
            (job_id,),
        ).fetchall()
    return {
        "job_id": str(job_id),
        "certificates": [
            {
                "certificate_id": str(r[0]),
                "recipient": r[1],
                "email": r[2],
                "status": r[3],
                "error": r[4],
                "download_url": f"/certificates/{r[0]}" if r[5] else None,
            }
            for r in rows
        ],
    }


@app.get("/certificates/{certificate_id}")
def download(certificate_id: uuid.UUID):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT c.file_path, c.recipient, j.course
               FROM certificates c JOIN jobs j ON j.id = c.job_id
               WHERE c.id = %s""",
            (certificate_id,),
        ).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(404, "certificate not found")
    path = Path(row[0])
    if not path.is_file():  # DB row without a file = lookup failure
        raise HTTPException(410, "certificate file missing")
    safe = row[1].replace("/", "_").replace("\\", "_")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{safe.replace(' ', '_')}_certificate.pdf")
