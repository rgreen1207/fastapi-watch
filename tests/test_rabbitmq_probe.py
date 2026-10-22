"""RabbitMQProbe's Management API enrichment, against a local stand-in for the API."""
import base64

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from fastapi_watch.probes import rabbitmq  # noqa: E402
from fastapi_watch.probes.rabbitmq import RabbitMQProbe  # noqa: E402


def _queue(i: int) -> dict:
    return {"name": f"q{i:04d}", "messages": i, "state": "running", "durable": True}


@pytest.fixture
async def management_api():
    """Serve /api/overview and /api/queues; record each request's query and auth header."""
    state = {"queues": [_queue(i) for i in range(500)], "paged": True, "requests": []}

    async def overview(request):
        state["requests"].append((request.path, dict(request.query), request.headers.get("Authorization")))
        return web.json_response({"object_totals": {"queues": len(state["queues"])}})

    async def queues(request):
        state["requests"].append((request.path, dict(request.query), request.headers.get("Authorization")))
        items = sorted(state["queues"], key=lambda q: q["messages"], reverse=True)
        if not state["paged"]:
            return web.json_response(state["queues"])
        size = int(request.query["page_size"])
        return web.json_response({"items": items[:size], "total_count": len(items), "page": 1})

    app = web.Application()
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/queues", queues)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state["url"] = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()


async def test_queues_are_paged_and_capped(management_api):
    # Every queue used to be fetched and stored: ~260 bytes each, per check,
    # in every history entry and every status response.
    probe = RabbitMQProbe(management_url=management_api["url"])
    details: dict = {}
    await probe._enrich_from_management_api(details)

    assert "management_api_error" not in details
    assert len(details["queues"]) == rabbitmq._MAX_QUEUES_LISTED
    assert next(iter(details["queues"])) == "q0499"  # deepest first
    queries = [query for path, query, _ in management_api["requests"] if path == "/api/queues"]
    assert queries == [{"page": "1", "page_size": str(rabbitmq._MAX_QUEUES_LISTED),
                        "sort": "messages", "sort_reverse": "true"}]


async def test_unpaged_queue_list_is_capped_deepest_first(management_api):
    management_api["paged"] = False  # a broker that ignores the paging parameters
    details: dict = {}
    await RabbitMQProbe(management_url=management_api["url"])._enrich_from_management_api(details)

    assert len(details["queues"]) == rabbitmq._MAX_QUEUES_LISTED
    assert list(details["queues"])[:2] == ["q0499", "q0498"]


async def test_percent_encoded_credentials_are_decoded_for_the_management_api(management_api):
    # aio-pika decodes the userinfo in the AMQP URL; the Basic header sent the
    # still-encoded form, so any password needing escaping got a 401.
    probe = RabbitMQProbe(url="amqp://ops%40corp:p%40ss%3Aw%2Frd@rabbit/", management_url=management_api["url"])
    await probe._enrich_from_management_api({})

    expected = "Basic " + base64.b64encode(b"ops@corp:p@ss:w/rd").decode()
    assert {auth for _, _, auth in management_api["requests"]} == {expected}
