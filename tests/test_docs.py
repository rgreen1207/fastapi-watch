"""Documentation examples must keep working.

Guards against doc drift: imports shown in the docs must exist, and the
RedisProbeStorage sketch in storage.py must actually satisfy ProbeStorage.
"""
import asyncio
import fnmatch
import importlib
import pathlib
import re
import textwrap

import pytest

from fastapi_watch import storage as storage_module
from fastapi_watch.models import AlertRecord, ProbeResult, ProbeStatus
from fastapi_watch.storage import ProbeStorage

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _documented_imports():
    files = [ROOT / "README.md", ROOT / "DOCS.md", *(ROOT / "src").rglob("*.py")]
    for path in files:
        text = path.read_text(encoding="utf-8")
        found = re.findall(r"^\s*from (fastapi_watch[\w.]*) import ([\w, ]+)\s*$", text, re.M)
        found += re.findall(r"^\s*from (fastapi_watch[\w.]*) import \(([^)]*)\)", text, re.M)
        for module, names in found:
            for name in re.split(r"[,\n]", names):
                name = name.split("#")[0].split(" as ")[0].strip()
                if name:
                    yield pytest.param(module, name, id=f"{path.name}:{module}.{name}")


@pytest.mark.parametrize("module, name", list(_documented_imports()))
def test_documented_import_exists(module, name):
    assert hasattr(importlib.import_module(module), name)


# ---------------------------------------------------------------------------
# The RedisProbeStorage sketch
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Just enough of redis.asyncio (decode_responses=True) to run the sketch."""

    def __init__(self):
        self.data: dict = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value

    async def keys(self, pattern):
        return [k for k in self.data if fnmatch.fnmatch(k, pattern)]

    async def mget(self, *keys):
        return [self.data.get(k) for k in keys]

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def rpush(self, key, value):
        self.data.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        items = self.data.get(key, [])
        self.data[key] = items[start:] if end == -1 else items[start:end + 1]

    async def expire(self, key, ttl):
        pass

    async def lrange(self, key, start, end):
        return list(self.data.get(key, []))


def _sketch_class():
    doc = ProbeStorage.__doc__
    code = textwrap.dedent(doc[doc.index("class RedisProbeStorage:"):])
    namespace = {"ProbeResult": ProbeResult, "AlertRecord": AlertRecord}
    exec(compile(code, "RedisProbeStorage-sketch", "exec"), namespace)  # documentation example under test
    return namespace["RedisProbeStorage"]


@pytest.mark.asyncio
async def test_redis_storage_sketch_works_inside_a_running_loop():
    store = _sketch_class()(_FakeRedis(), max_results=2)
    assert isinstance(store, ProbeStorage)

    for status in (ProbeStatus.HEALTHY, ProbeStatus.DEGRADED, ProbeStatus.UNHEALTHY):
        result = ProbeResult(name="db", status=status)
        await store.set_latest(result)
        await store.append_history(result)
    assert (await store.get_latest("db")).status == ProbeStatus.UNHEALTHY
    assert [r.status for r in (await store.get_history())["db"]] == [ProbeStatus.DEGRADED, ProbeStatus.UNHEALTHY]

    store.clear_latest()  # sync call while the loop runs — must not raise
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await store.get_all_latest() == {}


def test_redis_storage_sketch_clear_latest_without_a_loop():
    store = _sketch_class()(_FakeRedis())
    asyncio.run(store.set_latest(ProbeResult(name="db", status=ProbeStatus.HEALTHY)))
    store.clear_latest()
    assert asyncio.run(store.get_all_latest()) == {}


def test_storage_module_still_exports_the_protocol():
    assert storage_module.ProbeStorage is ProbeStorage
