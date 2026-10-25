import json
import uuid
from types import SimpleNamespace

import pytest

from app import db, documents, jobs, vault
from app.payments import FakeProvider
from app.worker import run_once

from .test_web import PNG_BYTES, submit, valid_form
from .trust_helpers import headers, operators


def test_quarantine_worker_then_manual_review(client):
    submit(client, valid_form())
    with db.transaction() as conn:
        bill = conn.execute("select * from bills").fetchone()
        reviewer = operators(conn)["reviewer"]
    assert client.get(f"/admin/uploads/{bill['id']}", headers=headers(reviewer)).status_code == 409
    assert run_once(FakeProvider()) is True
    response = client.get(f"/admin/uploads/{bill['id']}", headers=headers(reviewer))
    assert response.status_code == 200 and response.content == PNG_BYTES
    assert response.headers["content-security-policy"] == "sandbox"
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (bill["document_id"],)).fetchone()
        assert document["scan_status"] == "clean"
        assert document["extraction_status"] == "manual"
        assert conn.execute("select status from work_jobs").fetchone()["status"] == "done"
        assert bill["verification_id"] is None


@pytest.mark.parametrize("returncode, expected", [(1, "infected"), (2, "failed")])
def test_scanner_detection_and_failure_never_release_document(client, monkeypatch, settings, returncode, expected):
    monkeypatch.setattr(settings, "malware_scanner", "clamav")
    monkeypatch.setattr(documents.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=returncode))
    submit(client, valid_form())
    with db.transaction() as conn:
        bill = conn.execute("select * from bills").fetchone()
        reviewer = operators(conn)["reviewer"]
    run_once(FakeProvider())
    assert client.get(f"/admin/uploads/{bill['id']}", headers=headers(reviewer)).status_code == 409
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (bill["document_id"],)).fetchone()
        assert document["scan_status"] == expected
        assert document["extraction_ciphertext"] is None
        if expected == "failed":
            job = conn.execute("select * from work_jobs").fetchone()
            assert job["status"] == "pending" and job["last_error"] == "RuntimeError"


def test_textract_preserves_confidence_and_source_geometry_without_approving(settings, monkeypatch):
    import boto3
    monkeypatch.setattr(settings, "ocr_provider", "textract")
    blocks = [{"BlockType": "QUERY_RESULT", "Text": "$212.50", "Confidence": 98.1,
               "Geometry": {"BoundingBox": {"Left": 0.5, "Top": 0.5, "Width": 0.1, "Height": 0.1}}}]
    class Textract:
        def analyze_document(self, **kwargs):
            assert kwargs["Document"]["Bytes"] == PNG_BYTES
            assert "QUERIES" in kwargs["FeatureTypes"]
            return {"Blocks": blocks}
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Textract())
    status, ciphertext = documents.extract(PNG_BYTES, "image/png")
    assert status == "complete" and "$212.50" not in ciphertext
    assert json.loads(vault.decrypt(ciphertext)) == blocks
    assert documents.extract(PNG_BYTES, "image/heic") == ("manual", None)


def test_s3_documents_are_encrypted_private_and_integrity_checked(settings, monkeypatch):
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "private-test")
    monkeypatch.setattr(settings, "s3_kms_key_id", "kms-test")
    stored = {}
    class Body:
        def read(self, size):
            return stored["Body"]
        def close(self):
            pass
    class S3:
        def put_object(self, **kwargs):
            stored.update(kwargs)
        def get_object(self, **kwargs):
            assert kwargs["Bucket"] == "private-test"
            return {"Body": Body()}
    monkeypatch.setattr(documents, "_s3", lambda: S3())
    with db.transaction() as conn:
        document = documents.store(conn, PNG_BYTES, "image/png", "evidence")
    assert stored["ServerSideEncryption"] == "aws:kms" and stored["SSEKMSKeyId"] == "kms-test"
    assert "ACL" not in stored and stored["Body"] != PNG_BYTES
    assert documents.read(document) == PNG_BYTES
    with pytest.raises(ValueError, match="integrity"):
        documents.read({**document, "sha256": "0" * 64})


def test_stale_worker_cannot_complete_another_workers_lease():
    with db.transaction() as conn:
        jobs.enqueue(conn, "document", uuid.uuid4())
        first = jobs.claim(conn)
        conn.execute("update work_jobs set lease_until = now() - interval '1 minute' where id = %s", (first["id"],))
    with db.transaction() as conn:
        second = jobs.claim(conn)
        assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
        jobs.finish(conn, first)
        assert conn.execute("select status from work_jobs").fetchone()["status"] == "running"
        jobs.finish(conn, second)
        assert conn.execute("select status from work_jobs").fetchone()["status"] == "done"


def test_worker_crash_on_last_attempt_becomes_terminal_failure():
    with db.transaction() as conn:
        jobs.enqueue(conn, "document", uuid.uuid4())
        conn.execute("update work_jobs set status = 'running', attempts = 8, lease_until = now() - interval '1 minute'")
        assert jobs.claim(conn) is None
        row = conn.execute("select * from work_jobs").fetchone()
        assert row["status"] == "failed" and row["last_error"] == "LeaseExhausted"


def test_oversized_request_is_rejected_before_form_spooling(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 1024)
    response = client.post("/submit", content=b"x" * 70000, headers={"content-type": "multipart/form-data; boundary=test"})
    assert response.status_code == 413
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from documents").fetchone()["n"] == 0


def test_chunked_body_limit_does_not_trust_missing_content_length(client):
    response = client.post("/api/auth/dev", content=iter([b" " * 600000, b" " * 600000]),
                           headers={"content-type": "application/json"})
    assert response.status_code == 413
