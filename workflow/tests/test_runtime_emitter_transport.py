import asyncio

from app.runtime import events


class _Response:
    status_code = 200
    text = "ok"


class _FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_closed = False
        self.posts = []
        self.__class__.instances.append(self)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response()

    async def aclose(self):
        self.is_closed = True


def test_runtime_emitter_reuses_one_client_and_closes_it(monkeypatch):
    async def scenario():
        _FakeClient.instances.clear()
        monkeypatch.setattr(events.httpx, "AsyncClient", _FakeClient)
        emitter = events.RuntimeEmitter("run-1", "conv-1", "trace-1")

        await emitter.emit("run.started", payload={"stage": "start"})
        await emitter.emit("agent.started", agent_id="TechAgent")
        assert await emitter.emit_result({"status": "SUCCESS"}) is True

        assert len(_FakeClient.instances) == 1
        client = _FakeClient.instances[0]
        assert len(client.posts) == 3
        assert client.posts[0][0].endswith("/api/internal/agent-runs/events")
        assert client.posts[2][0].endswith("/api/internal/agent-runs/result")
        assert client.posts[0][1]["timeout"] == 10.0
        assert client.posts[2][1]["timeout"] == 15.0

        await emitter.aclose()
        assert client.is_closed is True

    asyncio.run(scenario())
