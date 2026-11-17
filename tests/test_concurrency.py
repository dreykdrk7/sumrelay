from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from payments.models import Attempt, Payment
from payments.services import create_payment
from payments.validation import PaymentError


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_key", [True, False])
def test_simultaneous_requests_create_one_checkout(installation, payload, gateway, same_key):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks; run the documented PostgreSQL test command.")
    barrier = Barrier(2)

    def submit(index):
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            attempt, _ = create_payment(payload, "key" if same_key else f"key-{index}", gateway)
            return str(attempt.id)
        except PaymentError as exc:
            assert exc.code == "attempt_blocked"
            return "blocked"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [0, 1]))
    assert gateway.creates == 1 and Payment.objects.count() == 1 and Attempt.objects.count() == 1
    if same_key:
        assert len(set(results)) == 1
    else:
        assert results.count("blocked") == 1
