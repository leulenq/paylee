"""Payment providers. The rest of the app only sees start_checkout, parse_webhook, and refund."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import stripe

from .config import Settings


@dataclass
class CheckoutStart:
    provider_ref: str
    # None means the payment already completed (the fake provider settles instantly).
    checkout_url: str | None
    payment_ref: str | None = None


@dataclass
class PaymentEvent:
    kind: str  # "paid" or "expired"
    contribution_id: str
    payment_ref: str | None = None
    amount_cents: int | None = None
    event_id: str | None = None
    provider_ref: str | None = None
    currency: str | None = None
    dispute_id: str | None = None
    dispute_status: str | None = None


@dataclass
class RefundResult:
    reference: str
    status: str
    amount_cents: int


class FakeProvider:
    """Development only: every payment succeeds immediately and no money moves."""

    name = "fake"

    def start_checkout(self, *, contribution_id, amount_cents, biller_name, expires_at) -> CheckoutStart:
        return CheckoutStart(
            provider_ref=f"fake_cs_{contribution_id}",
            checkout_url=None,
            payment_ref=f"fake_pi_{contribution_id}",
        )

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent | None:
        raise NotImplementedError("The fake provider has no webhooks")

    def cancel_checkout(self, provider_ref: str) -> None:
        pass

    def refund(self, payment_ref: str) -> None:
        pass


class StripeProvider:
    """Stripe Checkout in a web view. It supports Apple Pay and keeps card details off our servers."""

    name = "stripe"

    def __init__(self, secret_key: str, webhook_secret: str, public_base_url: str):
        self.client = stripe.StripeClient(secret_key)
        self.webhook_secret = webhook_secret
        self.public_base_url = public_base_url.rstrip("/")

    def start_checkout(self, *, contribution_id, amount_cents, biller_name, expires_at) -> CheckoutStart:
        return_url = f"{self.public_base_url}/checkout/return?contribution_id={contribution_id}"
        # Stripe rejects sessions that expire in under 30 minutes, so never ask for less.
        earliest = datetime.now(timezone.utc) + timedelta(minutes=31)
        session = self.client.v1.checkout.sessions.create(
            {
                "mode": "payment",
                "line_items": [
                    {
                        "quantity": 1,
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": amount_cents,
                            "product_data": {"name": f"Toward a {biller_name} bill"},
                        },
                    }
                ],
                "client_reference_id": str(contribution_id),
                "metadata": {"contribution_id": str(contribution_id)},
                "expires_at": int(max(expires_at, earliest).timestamp()),
                "success_url": f"{return_url}&result=success",
                "cancel_url": f"{return_url}&result=cancel",
                "payment_method_types": ["card"],
            },
            options={"idempotency_key": f"paylee-checkout-{contribution_id}"},
        )
        return CheckoutStart(provider_ref=session.id, checkout_url=session.url)

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent | None:
        # Raises stripe.SignatureVerificationError for anything Stripe didn't sign.
        event = self.client.construct_event(payload, signature, self.webhook_secret)
        session = event.data.object
        if event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and session.payment_status == "paid":
            return PaymentEvent(
                kind="paid",
                contribution_id=session.client_reference_id,
                payment_ref=session.payment_intent,
                amount_cents=session.amount_total,
                event_id=event.id, provider_ref=session.id, currency=getattr(session, "currency", None),
            )
        if event.type == "checkout.session.expired":
            return PaymentEvent(kind="expired", contribution_id=session.client_reference_id,
                                event_id=event.id, provider_ref=session.id)
        if event.type in ("charge.dispute.created", "charge.dispute.closed"):
            return PaymentEvent(kind="dispute", contribution_id="", event_id=event.id,
                                payment_ref=getattr(session, "payment_intent", None), provider_ref=getattr(session, "charge", None),
                                dispute_id=session.id, amount_cents=session.amount,
                                dispute_status="open" if event.type.endswith("created") else session.status)
        return None

    def cancel_checkout(self, provider_ref: str) -> None:
        # Closes the payment page so an abandoned checkout can't be completed later.
        self.client.v1.checkout.sessions.expire(provider_ref)

    def refund(self, payment_ref: str) -> RefundResult:
        # Recover an accepted request after a timeout/crash, including beyond Stripe's idempotency window.
        existing = self.client.v1.refunds.list({"payment_intent": payment_ref, "limit": 100})
        active = [r for r in existing.data if r.status not in ("failed", "canceled")]
        if len(active) > 1:
            raise RuntimeError("Multiple refunds require treasury reconciliation")
        refund = active[0] if active else self.client.v1.refunds.create(
            {"payment_intent": payment_ref}, options={"idempotency_key": f"paylee-full-refund-{payment_ref}"})
        return RefundResult(refund.id, refund.status, refund.amount)


def build_provider(settings: Settings) -> FakeProvider | StripeProvider:
    if settings.payment_provider == "stripe":
        if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
            raise RuntimeError("PAYMENT_PROVIDER=stripe needs STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET")
        if settings.stripe_secret_key.startswith("sk_live_") and not settings.live_payments_approved:
            raise RuntimeError("Live collection requires explicit approved-program configuration")
        return StripeProvider(settings.stripe_secret_key, settings.stripe_webhook_secret, settings.public_base_url)
    if settings.payment_provider == "fake":
        return FakeProvider()
    raise RuntimeError(f"Unknown PAYMENT_PROVIDER {settings.payment_provider!r}")
