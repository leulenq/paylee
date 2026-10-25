"""Balanced append-only accounting. All postings share their business transaction."""


def post(conn, *, key, bill_id, debit, credit, amount, contribution_id=None, order_id=None):
    if amount <= 0:
        raise ValueError("A journal amount must be positive")
    existing = conn.execute("select * from ledger_journals where event_key = %s", (key,)).fetchone()
    if existing:
        if (existing["debit_account"], existing["credit_account"], existing["amount_cents"], str(existing["bill_id"]),
                str(existing["contribution_id"]), str(existing["payment_order_id"])) != (
            debit, credit, amount, str(bill_id), str(contribution_id), str(order_id)
        ):
            raise ValueError("Idempotency key reused with different journal data")
        return existing
    return conn.execute(
        """insert into ledger_journals
           (event_key, bill_id, contribution_id, payment_order_id, debit_account, credit_account, amount_cents)
           values (%s, %s, %s, %s, %s, %s, %s) returning *""",
        (key, bill_id, contribution_id, order_id, debit, credit, amount),
    ).fetchone()


def balances(conn, bill_id):
    return {r["account"]: r["balance"] for r in conn.execute(
        "select account, sum(signed_cents)::bigint as balance from ledger_lines where bill_id = %s group by account",
        (bill_id,),
    ).fetchall()}


def collect(conn, contribution_id, bill_id, amount):
    post(conn, key=f"collection:{contribution_id}", bill_id=bill_id, contribution_id=contribution_id,
         debit="collection_receivable", credit="restricted_payable", amount=amount)


def refund(conn, contribution):
    amount = contribution["received_amount_cents"]
    if not amount:
        raise ValueError("Cannot refund an unrecorded collection")
    post(conn, key=f"refund:{contribution['id']}", bill_id=contribution["bill_id"],
         contribution_id=contribution["id"], debit="restricted_payable",
         credit="program_cash" if contribution["funds_reconciled_at"] else "collection_receivable", amount=amount)
