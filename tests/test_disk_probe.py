import pytest
from unittest.mock import patch, MagicMock
from fastapi_watch.probes.disk import DiskProbe
from fastapi_watch.models import ProbeStatus


def _make_usage(total_gb: float, used_gb: float):
    total = int(total_gb * 1024 ** 3)
    used = int(used_gb * 1024 ** 3)
    free = total - used
    Usage = MagicMock()
    Usage.total = total
    Usage.used = used
    Usage.free = free
    return Usage


@pytest.mark.asyncio
async def test_disk_healthy_below_warn():
    probe = DiskProbe(warn_percent=80.0, fail_percent=90.0)
    with patch("shutil.disk_usage", return_value=_make_usage(100, 50)):
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["percent_used"] == pytest.approx(50.0, abs=0.1)


@pytest.mark.asyncio
async def test_disk_degraded_at_warn():
    probe = DiskProbe(warn_percent=80.0, fail_percent=90.0)
    with patch("shutil.disk_usage", return_value=_make_usage(100, 85)):
        result = await probe.check()
    assert result.status == ProbeStatus.DEGRADED


@pytest.mark.asyncio
async def test_disk_unhealthy_at_fail():
    probe = DiskProbe(warn_percent=80.0, fail_percent=90.0)
    with patch("shutil.disk_usage", return_value=_make_usage(100, 95)):
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_disk_details_fields():
    probe = DiskProbe(path="/data")
    with patch("shutil.disk_usage", return_value=_make_usage(100, 50)):
        result = await probe.check()
    assert result.details["path"] == "/data"
    assert "total_gb" in result.details
    assert "used_gb" in result.details
    assert "free_gb" in result.details
    assert "percent_used" in result.details


@pytest.mark.asyncio
async def test_disk_unhealthy_on_exception():
    probe = DiskProbe(path="/nonexistent")
    with patch("shutil.disk_usage", side_effect=FileNotFoundError("no such path")):
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "FileNotFoundError" in result.error


@pytest.mark.asyncio
async def test_disk_custom_name():
    probe = DiskProbe(name="uploads-disk")
    with patch("shutil.disk_usage", return_value=_make_usage(100, 10)):
        result = await probe.check()
    assert result.name == "uploads-disk"
