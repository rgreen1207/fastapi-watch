"""Settings that would leave a probe silently recording nothing are rejected up front."""
import pytest

from fastapi_watch.probes import FastAPIRouteProbe, FastAPIWebSocketProbe, PassiveProbe, RedisProbe

_PASSIVE = [
    lambda **kw: PassiveProbe("p", **kw),
    lambda **kw: FastAPIRouteProbe(**kw),
    lambda **kw: RedisProbe(**kw),
]
_ALL = _PASSIVE + [lambda **kw: FastAPIWebSocketProbe(**kw)]


@pytest.mark.parametrize("make", _ALL)
@pytest.mark.parametrize("kwargs, match", [
    ({"window_size": 0}, "window_size"),
    ({"window_size": -5}, "window_size"),
    ({"ema_alpha": 0}, "ema_alpha"),
    ({"ema_alpha": -0.1}, "ema_alpha"),
    ({"ema_alpha": 1.5}, "ema_alpha"),
    ({"max_error_rate": -0.01}, "max_error_rate"),
    ({"max_error_rate": 1.01}, "max_error_rate"),
])
def test_invalid_settings_raise(make, kwargs, match):
    with pytest.raises(ValueError, match=match):
        make(**kwargs)


@pytest.mark.parametrize("make", _PASSIVE)
@pytest.mark.parametrize("size", [0, -1])
def test_invalid_cache_window_size_raises(make, size):
    with pytest.raises(ValueError, match="cache_window_size"):
        make(cache_window_size=size)


@pytest.mark.parametrize("make", _ALL)
def test_boundary_values_are_accepted(make):
    make(window_size=1, ema_alpha=1.0, max_error_rate=0.0)
    make(max_error_rate=1.0, ema_alpha=0.001)


async def test_smallest_windows_still_record():
    probe = PassiveProbe("p", window_size=1, cache_window_size=1)

    @probe.watch
    def call():
        return 1

    call()
    call()
    result = await probe.check()
    assert result.details["call_count"] == 2
    assert result.details["p50_rtt_ms"] is not None
