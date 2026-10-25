"""Original-source full refunds. Pending provider requests are not reported as completed."""

from . import bills, db, trust
from .config import get_settings


def process(provider, contribution_id):
    with db.transaction() as conn:
        row = conn.execute("select * from contributions where id = %s", (contribution_id,)).fetchone()
        if not row or row["status"] == "refunded":
            return
        if row["status"] != "refund_pending" or not row["payment_ref"]:
            raise trust.TrustError("Contribution is not awaiting an original-source refund")
        if row["provider"] != provider.name:
            raise trust.TrustError("The configured provider cannot refund this contribution")
        if conn.execute("select 1 from funding_disputes where contribution_id = %s and status <> 'won'",
                        (contribution_id,)).fetchone():
            raise trust.TrustError("Do not refund disputed funding twice; treasury review is required")
    result = provider.refund(row["payment_ref"])
    if result is None:
        if get_settings().environment == "production":
            raise trust.TrustError("Provider did not confirm the refund")
        completed = True  # Explicit local fake provider and test doubles only.
    else:
        if result.amount_cents != row["received_amount_cents"]:
            raise trust.TrustError("Partial or mismatched refund requires treasury reconciliation")
        completed = result.status == "succeeded"
        with db.transaction() as conn:
            conn.execute("update contributions set refund_ref = %s where id = %s", (result.reference, contribution_id))
    if not completed:
        raise trust.TrustError("Refund is pending provider confirmation")
    with db.transaction() as conn:
        bills.mark_refunded(conn, contribution_id)
