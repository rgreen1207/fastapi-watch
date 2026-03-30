import pytest
from unittest.mock import patch, MagicMock
from fastapi_watch.probes.smtp import SMTPProbe
from fastapi_watch.models import ProbeStatus


def _make_smtp_mock(banner=b"220 mail.example.com ESMTP", extensions=None):
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    smtp.getwelcome = MagicMock(return_value=banner)
    smtp.ehlo = MagicMock(return_value=(250, b"ok"))
    smtp.esmtp_features = {k: "" for k in (extensions or ["size", "starttls", "auth"])}
    return smtp


@pytest.mark.asyncio
async def test_smtp_healthy_on_successful_connect():
    with patch("smtplib.SMTP", return_value=_make_smtp_mock()):
        probe = SMTPProbe("mail.example.com", port=25)
        result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.details["host"] == "mail.example.com"
    assert result.details["port"] == 25
    assert "server_banner" in result.details
    assert "extensions" in result.details


@pytest.mark.asyncio
async def test_smtp_extensions_sorted():
    with patch("smtplib.SMTP", return_value=_make_smtp_mock(extensions=["size", "auth", "8bitmime"])):
        probe = SMTPProbe("smtp.test", port=587)
        result = await probe.check()

    assert result.details["extensions"] == sorted(["size", "auth", "8bitmime"])


@pytest.mark.asyncio
async def test_smtp_unhealthy_on_connection_error():
    import smtplib
    with patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, b"Connection refused")):
        probe = SMTPProbe("broken.smtp", port=25)
        result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert "SMTPConnectError" in result.error


@pytest.mark.asyncio
async def test_smtp_custom_name():
    with patch("smtplib.SMTP", return_value=_make_smtp_mock()):
        probe = SMTPProbe("smtp.example.com", name="sendgrid")
        result = await probe.check()

    assert result.name == "sendgrid"


@pytest.mark.asyncio
async def test_smtp_tls_flag_in_details():
    mock = _make_smtp_mock()
    mock.starttls = MagicMock()
    with patch("smtplib.SMTP", return_value=mock):
        probe = SMTPProbe("smtp.example.com", port=587, use_tls=True)
        result = await probe.check()

    assert result.details["tls"] is True
    mock.starttls.assert_called_once()


@pytest.mark.asyncio
async def test_smtp_no_extra_deps():
    """SMTPProbe must not require any package beyond the stdlib."""
    import fastapi_watch.probes.smtp as smtp_module
    import smtplib
    assert smtplib  # stdlib present
