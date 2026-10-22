"""Documentation examples must keep working.

Guards against doc drift: imports shown in the docs must exist, the custom
backend outline in storage.py must satisfy ProbeStorage, and links between
README.md and DOCS.md must point at headings that exist.
"""
import importlib
import pathlib
import re
import textwrap

import pytest

from fastapi_watch import RedisProbeStorage
from fastapi_watch import storage as storage_module
from fastapi_watch.storage import InMemoryProbeStorage, ProbeStorage

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
# Storage backend docs
# ---------------------------------------------------------------------------

def _outline_class():
    doc = ProbeStorage.__doc__
    code = textwrap.dedent(doc[doc.index("class MyStorage:"):doc.index("Keep every collection")])
    namespace: dict = {}
    exec(compile(code, "MyStorage-outline", "exec"), namespace)  # documentation example under test
    return namespace["MyStorage"]


def test_custom_backend_outline_satisfies_the_protocol():
    assert isinstance(_outline_class()(), ProbeStorage)


def test_bundled_backends_satisfy_the_protocol():
    assert isinstance(InMemoryProbeStorage(), ProbeStorage)
    assert isinstance(RedisProbeStorage(object()), ProbeStorage)  # no I/O at construction


def test_optional_protocol_methods_are_not_required():
    # Backends written before configure()/forget()/claim_alert() existed must still qualify.
    class Legacy:
        async def get_latest(self, name): ...
        async def get_all_latest(self): ...
        async def set_latest(self, result): ...
        def clear_latest(self): ...
        async def append_history(self, result): ...
        async def get_history(self): ...
        async def append_alert(self, alert): ...
        async def get_alerts(self): ...

    assert isinstance(Legacy(), ProbeStorage)


def _anchor(heading: str) -> str:
    """GitHub's heading-to-anchor rule, for the plain headings these docs use."""
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


@pytest.mark.parametrize("source", ["README.md", "DOCS.md"])
def test_cross_document_anchors_exist(source):
    text = (ROOT / source).read_text(encoding="utf-8")
    for target, anchor in re.findall(r"\]\((?:[^)\s#]*/)?((?:README|DOCS)\.md)#([\w-]+)\)", text):
        headings = re.findall(r"^#+ (.+)$", (ROOT / target).read_text(encoding="utf-8"), re.M)
        assert anchor in {_anchor(h) for h in headings}, f"{source} links to missing {target}#{anchor}"


def test_storage_module_still_exports_the_protocol():
    assert storage_module.ProbeStorage is ProbeStorage


# ---------------------------------------------------------------------------
# Constructor calls in examples
# ---------------------------------------------------------------------------

def _public_classes() -> dict:
    import inspect as _inspect

    import fastapi_watch
    import fastapi_watch.probes

    found = {}
    for module in (fastapi_watch, fastapi_watch.probes):
        for name in module.__all__:
            obj = getattr(module, name)
            if _inspect.isclass(obj):
                found[name] = obj
    return found


def _accepted_keywords(cls) -> set[str] | None:
    """Keyword names ``cls(...)`` accepts, following ``**kwargs`` up the MRO; None = any."""
    import inspect as _inspect

    accepted: set[str] = set()
    for klass in cls.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        params = _inspect.signature(init).parameters.values()
        accepted |= {
            p.name for p in params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and p.name != "self"
        }
        if not any(p.kind is p.VAR_KEYWORD for p in params):
            return accepted
    return None


def _documented_constructor_calls():
    import ast

    classes = _public_classes()
    for path in (ROOT / "README.md", ROOT / "DOCS.md"):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"```python\n(.*?)```", text, re.S):
            try:
                tree = ast.parse(textwrap.dedent(block))
            except SyntaxError:
                continue  # fragments (e.g. a bare argument list) are not checked
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in classes:
                    keywords = sorted(k.arg for k in node.keywords if k.arg is not None)
                    yield pytest.param(
                        node.func.id, keywords,
                        id=f"{path.name}:{node.func.id}({', '.join(keywords)})",
                    )


@pytest.mark.parametrize("class_name, keywords", sorted(
    {(p.values[0], tuple(p.values[1])): p for p in _documented_constructor_calls()}.values(),
    key=lambda p: p.id,
))
def test_documented_constructor_keywords_exist(class_name, keywords):
    accepted = _accepted_keywords(_public_classes()[class_name])
    if accepted is not None:
        assert not set(keywords) - accepted, f"{class_name} does not accept {set(keywords) - accepted}"
