"""Run `uv run python -m app.worker`. Leases recover work after a process crash."""

import logging
import signal
import threading

from . import db, documents, events, jobs, payments, refunds
from .config import get_settings

log = logging.getLogger(__name__)


def run_once(provider):
    with db.transaction() as conn:
        job = jobs.claim(conn)
    if not job:
        return False
    error = None
    try:
        if job["kind"] == "document":
            documents.process(job["object_id"])
        elif job["kind"] == "provider_event":
            events.process(provider, job["object_id"])
        elif job["kind"] == "refund":
            refunds.process(provider, job["object_id"])
    except Exception as exc:
        error = exc
        log.warning("Job %s failed (%s)", job["id"], type(exc).__name__)
    with db.transaction() as conn:
        jobs.finish(conn, job, error)
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    settings.check_safe_for_environment()
    provider = payments.build_provider(settings)
    db.open_pool(settings.database_url)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        if settings.schema_auto_apply:
            with db.transaction() as conn:
                db.init_schema(conn)
        while not stopped.is_set():
            if not run_once(provider):
                stopped.wait(settings.worker_poll_seconds)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
