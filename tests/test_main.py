"""Tests for the bulk certificate generator.

Single file, no fixtures: each test wraps TestClient in `with` so the lifespan
(startup: create tables, cert dir) runs, and the background job completes when
the `with` block exits. Postgres must be reachable at DATABASE_URL (README).
"""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://certuser:certpass@localhost:5433/certdb")
os.environ.setdefault("CERT_DIR", "certificates-test")

from fastapi.testclient import TestClient

import main
from main import app

client = TestClient(app)


def _job(course="Ponytail 101", names=("Alice", "Bob")):
    return client.post(
        "/certificates/generate",
        json={"course": course,
              "recipients": [{"name": n, "email": f"{n.lower()}@x.io"} for n in names]},
    )


def test_create_job():
    with client:
        r = _job()
        job_id = r.json()["job_id"]
        assert r.status_code == 202
        uuid.UUID(job_id)  # parses
        assert r.json()["status"] == "pending"


def test_input_validation():
    with client:
        # empty recipients
        assert client.post("/certificates/generate", json={"course": "C", "recipients": []}).status_code == 422
        # bad email
        r = client.post("/certificates/generate", json={
            "course": "C", "recipients": [{"name": "A", "email": "not-an-email"}]})
        assert r.status_code == 422
        # blank name
        r = client.post("/certificates/generate", json={
            "course": "C", "recipients": [{"name": "", "email": "a@x.io"}]})
        assert r.status_code == 422
        # duplicate emails (case-insensitive)
        r = client.post("/certificates/generate", json={"course": "C", "recipients": [
            {"name": "A", "email": "dup@x.io"}, {"name": "B", "email": "DUP@x.io"}]})
        assert r.status_code == 422
        assert "duplicate" in r.text


def test_job_completes_and_certificates_download():
    with client:
        r = _job(names=("Carol", "Dave"))  # 2 recipients
        job_id = r.json()["job_id"]
    # background task ran when the `with` block exited

    status = client.get(f"/certificates/jobs/{job_id}").json()
    assert status["status"] == "completed"
    assert status["total"] == 2 and status["completed"] == 2 and status["failed"] == 0
    assert status["certificates"] == 2

    listing = client.get(f"/certificates/jobs/{job_id}/certificates").json()
    certs = listing["certificates"]
    assert len(certs) == 2
    assert all(c["status"] == "success" and c["error"] is None for c in certs)

    dl = client.get(f"/certificates/{certs[0]['certificate_id']}")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    assert dl.content[:5] == b"%PDF-"  # real PDF magic bytes
    assert dl.headers["content-disposition"].endswith('.pdf"')


def test_individual_failure_isolated():
    # #ponytail: simplest failure injection is monkeypatching render_pdf.
    orig = main.render_pdf
    try:
        main.render_pdf = lambda *a, **k: (_ for _ in ()).throw(OSError("disk exploded"))
        with client:
            r = _job(names=("Failing",))
            job_id = r.json()["job_id"]
        status = client.get(f"/certificates/jobs/{job_id}").json()
        assert status["status"] == "completed"  # job completes, cert fails
        assert status["failed"] == 1 and status["completed"] == 0

        listing = client.get(f"/certificates/jobs/{job_id}/certificates").json()
        cert = listing["certificates"][0]
        assert cert["status"] == "failed"
        assert "disk exploded" in cert["error"]
    finally:
        main.render_pdf = orig

    # valid recipients in the same job still succeed (failure isolation)
    with client:
        r = _job(names=("Good1", "Good2"))
        job_id = r.json()["job_id"]
    status = client.get(f"/certificates/jobs/{job_id}").json()
    assert status["completed"] == 2 and status["failed"] == 0


def test_chunked_progress_totals():
    # 150 recipients crosses the 100-chunk flush boundary in run_job; if the
    # chunk loop dropped anything, counters/certificates would not equal 150.
    names = tuple(f"Bulk{i}" for i in range(150))
    with client:
        job_id = _job(course="Scale", names=names).json()["job_id"]
    status = client.get(f"/certificates/jobs/{job_id}").json()
    assert status["status"] == "completed"
    assert status["completed"] == 150 and status["failed"] == 0
    assert status["certificates"] == 150
    listing = client.get(f"/certificates/jobs/{job_id}/certificates").json()
    assert len(listing["certificates"]) == 150


def test_job_not_found():
    with client:
        pass  # ensure tables exist even if this test runs alone
    missing = str(uuid.uuid4())
    assert client.get(f"/certificates/jobs/{missing}").status_code == 404
    assert client.get(f"/certificates/jobs/{missing}/certificates").status_code == 404
    assert client.get(f"/certificates/{missing}").status_code == 404
