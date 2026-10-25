"""Private encrypted artifacts, fail-closed malware scanning, and optional AWS OCR.

OCR is evidence for a reviewer, never a payment approval. No URL from a document is fetched.
"""

import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import db, jobs, vault
from .config import get_settings


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", region_name=get_settings().aws_region,
                        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}))


def _local_path(key):
    # Keys are generated here, never taken from filenames or extracted bill text.
    uuid.UUID(key.removesuffix(".enc"))
    return Path(get_settings().upload_dir) / "private" / key


def store(conn, content: bytes, content_type: str, purpose: str):
    settings = get_settings()
    if not content or len(content) > settings.max_upload_bytes:
        raise ValueError("Invalid document size")
    document_id = uuid.uuid4()
    key = f"{document_id}.enc"
    encrypted = vault.encrypt_bytes(content)
    if settings.storage_provider == "s3":
        _s3().put_object(Bucket=settings.s3_bucket, Key=f"private/{key}", Body=encrypted,
                         ContentType="application/octet-stream", ServerSideEncryption="aws:kms",
                         SSEKMSKeyId=settings.s3_kms_key_id)
    elif settings.storage_provider == "local" and settings.environment != "production":
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(encrypted)
    else:
        raise RuntimeError("Unsupported document storage")
    row = conn.execute(
        """insert into documents (id, storage_key, sha256, content_type, size_bytes, purpose)
           values (%s, %s, %s, %s, %s, %s) returning *""",
        (document_id, key, hashlib.sha256(content).hexdigest(), content_type, len(content), purpose),
    ).fetchone()
    jobs.enqueue(conn, "document", document_id)
    return row


def read(document):
    settings = get_settings()
    if settings.storage_provider == "s3":
        body = _s3().get_object(Bucket=settings.s3_bucket, Key=f"private/{document['storage_key']}")["Body"]
        try:
            encrypted = body.read(settings.max_upload_bytes * 2 + 1024)
        finally:
            body.close()
    else:
        encrypted = _local_path(document["storage_key"]).read_bytes()
    content = vault.decrypt_bytes(encrypted)
    if len(content) != document["size_bytes"] or hashlib.sha256(content).hexdigest() != document["sha256"]:
        raise ValueError("Document integrity mismatch")
    return content


def scan(content):
    settings = get_settings()
    if settings.malware_scanner == "development" and settings.environment != "production":
        return "clean", "development-unverified"
    if settings.malware_scanner != "clamav":
        raise RuntimeError("Unsupported malware scanner")
    if settings.clamd_host:
        # ClamAV INSTREAM: length-prefixed bytes, never a remote URL or shared file path.
        with socket.create_connection((settings.clamd_host, settings.clamd_port), timeout=10) as connection:
            connection.settimeout(90)
            connection.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), 65536):
                chunk = content[offset:offset + 65536]
                connection.sendall(struct.pack("!I", len(chunk)) + chunk)
            connection.sendall(struct.pack("!I", 0))
            response = b""
            while b"\0" not in response and len(response) < 4096:
                part = connection.recv(4096)
                if not part:
                    break
                response += part
        verdict = response.rstrip(b"\0\n")
        if verdict == b"stream: OK":
            return "clean", "clamav"
        if verdict.startswith(b"stream: ") and verdict.endswith(b" FOUND"):
            return "infected", "clamav"
        raise RuntimeError("Malware scanner returned an unknown result")
    with tempfile.NamedTemporaryFile(prefix="paylee-scan-") as file:
        file.write(content)
        file.flush()
        result = subprocess.run([settings.clamscan_path, "--no-summary", "--", file.name],
                                capture_output=True, timeout=90, check=False)
    if result.returncode == 0:
        return "clean", "clamav"
    if result.returncode == 1:
        return "infected", "clamav"
    raise RuntimeError("Malware scanner unavailable or definitions are invalid")


def extract(content, content_type):
    settings = get_settings()
    if settings.ocr_provider == "manual":
        return "manual", None
    if settings.ocr_provider != "textract":
        raise RuntimeError("Unsupported OCR provider")
    # Synchronous Textract does not support HEIC or multi-page PDFs. Those remain manual.
    if content_type == "image/heic":
        return "manual", None
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    client = boto3.client("textract", region_name=settings.aws_region,
                          config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}))
    queries = [
        ("Who is the biller?", "biller"), ("What is the customer account number?", "account"),
        ("What is the invoice number?", "invoice"), ("What is the total amount due?", "balance"),
        ("What is the payment due date?", "due_date"), ("What is the billing period?", "period"),
    ]
    try:
        response = client.analyze_document(Document={"Bytes": content}, FeatureTypes=["FORMS", "TABLES", "QUERIES"],
                                           QueriesConfig={"Queries": [{"Text": q, "Alias": a} for q, a in queries]})
    except ClientError as error:
        if error.response["Error"]["Code"] in {"UnsupportedDocumentException", "BadDocumentException", "DocumentTooLargeException"}:
            return "manual", None
        raise
    # Keep source geometry and confidence. A high score is not a verified debt.
    return "complete", vault.encrypt(json.dumps(response["Blocks"]))


def process(document_id):
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (document_id,)).fetchone()
    if not document:
        raise LookupError("Missing document")
    if document["scan_status"] == "infected":
        return
    content = read(document)
    try:
        status, provider = scan(content)
    except Exception:
        with db.transaction() as conn:
            conn.execute("update documents set scan_status = 'failed' where id = %s", (document_id,))
        raise
    with db.transaction() as conn:
        conn.execute("update documents set scan_status = %s, scan_provider = %s, scanned_at = now() where id = %s",
                     (status, provider, document_id))
    if status == "infected":
        return
    try:
        extraction_status, extracted = extract(content, document["content_type"]) if document["purpose"] == "bill" else ("manual", None)
    except Exception:
        with db.transaction() as conn:
            conn.execute("update documents set extraction_status = 'failed' where id = %s", (document_id,))
        raise
    with db.transaction() as conn:
        conn.execute("update documents set extraction_status = %s, extraction_ciphertext = %s where id = %s",
                     (extraction_status, extracted, document_id))
