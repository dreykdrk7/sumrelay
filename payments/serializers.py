def iso(value):
    return value.isoformat() if value else None


def serialize_attempt(attempt):
    return {
        "id": str(attempt.id),
        "status": attempt.status,
        "uncertain": attempt.uncertain,
        "closed_confirmed": attempt.closed_confirmed,
        "amount": format(attempt.payment.amount, ".2f"),
        "currency": attempt.payment.currency,
        "merchant_code": attempt.payment.merchant_code,
        "sumup_checkout_id": attempt.sumup_id,
        "sumup_checkout_reference": str(attempt.id),
        "checkout_url": attempt.checkout_url or None,
        "transaction_id": attempt.transaction_id or None,
        "transaction_code": attempt.transaction_code or None,
        "refunded_amount": format(attempt.refunded_amount, ".2f"),
        "refund_events": [
            {
                "id": event.provider_id,
                "amount": format(event.amount, ".2f"),
                "status": event.status,
                "occurred_at": iso(event.occurred_at),
            }
            for event in attempt.refund_events.order_by("provider_id")
        ],
        "paid_at": iso(attempt.paid_at),
        "created_at": iso(attempt.created_at),
        "updated_at": iso(attempt.updated_at),
        "expires_at": iso(attempt.expires_at),
        "last_checked_at": iso(attempt.last_checked_at),
        "last_error": attempt.last_error or None,
        **({"action_required": attempt.action_required} if attempt.payment.saved_card_id else {}),
    }


def serialize_payment(payment):
    attempts = [serialize_attempt(a) for a in payment.attempts.order_by("created_at")]
    latest = attempts[-1] if attempts else {}
    paid = [a for a in attempts if a["paid_at"]]
    current = paid[-1] if paid else latest
    # A late provider result can concern an older attempt. Keep the original charge visible.
    unresolved_sibling = bool(paid) and any(
        not a["closed_confirmed"] for a in attempts if not a["paid_at"]
    )
    uncertain = any(a["uncertain"] for a in attempts) or len(paid) > 1 or unresolved_sibling
    return {
        "id": str(payment.id),
        "reference": payment.reference,
        "amount": format(payment.amount, ".2f"),
        "currency": payment.currency,
        "merchant_code": payment.merchant_code,
        "status": current.get("status", "cancelled" if payment.cancel_requested_at else "pending"),
        "uncertain": uncertain if attempts else not bool(payment.cancel_requested_at),
        "checkout_url": None if paid or uncertain else latest.get("checkout_url"),
        "latest_attempt_id": latest.get("id"),
        "refunded_amount": current.get("refunded_amount", "0.00"),
        "created_at": iso(payment.created_at),
        "attempts": attempts,
        **(
            {
                "saved_card_id": str(payment.saved_card_id),
                "cancel_requested": bool(payment.cancel_requested_at),
            }
            if payment.saved_card_id
            else {}
        ),
    }
