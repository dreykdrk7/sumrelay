from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from payments.gateway import ProviderRejected, SumUpGateway


def test_sandbox_mode_blocks_a_live_merchant_before_checkout_creation(settings):
    settings.SUMUP_SANDBOX_ONLY = True
    attempt = SimpleNamespace(payment=SimpleNamespace(merchant_code="MLIVE"))
    with SumUpGateway() as gateway:
        with (
            patch.object(
                type(gateway.client.merchants),
                "get",
                return_value=SimpleNamespace(
                    merchant_code="MLIVE",
                    default_currency="EUR",
                    sandbox=False,
                ),
            ),
            patch.object(type(gateway.client.checkouts), "create") as create,
        ):
            with pytest.raises(ProviderRejected, match="sandbox"):
                gateway.create(attempt)
            create.assert_not_called()


def test_credential_check_only_reads_account_and_does_not_print_key(settings):
    output = StringIO()
    with patch("payments.management.commands.check_sumup.SumUpGateway") as factory:
        gateway = factory.return_value.__enter__.return_value
        gateway.account.return_value = {
            "merchant_code": settings.SUMUP_MERCHANT_CODE,
            "currency": "EUR",
            "sandbox": True,
        }
        call_command("check_sumup", require_sandbox=True, stdout=output)
        gateway.account.assert_called_once_with(settings.SUMUP_MERCHANT_CODE)
        gateway.find.assert_called_once()
        assert gateway.find.call_args.args[0].startswith("connection-check-")
        gateway.create.assert_not_called()
    assert "Credenciales aceptadas" in output.getvalue()
    assert settings.SUMUP_API_KEY not in output.getvalue()


def test_credential_check_rejects_unconfirmed_sandbox(settings):
    with patch("payments.management.commands.check_sumup.SumUpGateway") as factory:
        factory.return_value.__enter__.return_value.account.return_value = {
            "merchant_code": settings.SUMUP_MERCHANT_CODE,
            "currency": "EUR",
            "sandbox": None,
        }
        with pytest.raises(CommandError, match="no está confirmado"):
            call_command("check_sumup", require_sandbox=True)


def test_credential_check_explains_missing_credentials(settings):
    settings.SUMUP_API_KEY = ""
    with pytest.raises(CommandError, match="Rellena SUMUP_API_KEY"):
        call_command("check_sumup", require_sandbox=True)


@pytest.mark.parametrize("sandbox", [False, None])
def test_connection_only_accepts_read_access_without_changing_payment_guard(settings, sandbox):
    settings.SUMUP_SANDBOX_ONLY = True
    output = StringIO()
    with patch("payments.management.commands.check_sumup.SumUpGateway") as factory:
        gateway = factory.return_value.__enter__.return_value
        gateway.account.return_value = {
            "merchant_code": settings.SUMUP_MERCHANT_CODE,
            "currency": "EUR",
            "sandbox": sandbox,
        }
        gateway.find.return_value = []
        call_command("check_sumup", connection_only=True, stdout=output)
        gateway.find.assert_called_once()
        gateway.create.assert_not_called()
    assert settings.SUMUP_SANDBOX_ONLY is True
    assert "Credenciales aceptadas" in output.getvalue()
    assert "bloqueo no se ha cambiado" in output.getvalue()
    assert settings.SUMUP_API_KEY not in output.getvalue()
    if sandbox is None:
        assert "no informado por SumUp" in output.getvalue()
    else:
        assert "comercio real" in output.getvalue()
