import pytest
from Orchestrator.sms.manager import AMIConnectionManager


class FakeClient:
    def __init__(self, host, port, username, secret):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True
        self.connected = False


def _gw(id, ip, enabled=True, numbers=None):
    numbers = numbers or []
    ports = [
        {
            "span": 2 + i,
            "slot": i,
            "phone_number": (numbers[i] if i < len(numbers) else ""),
            "carrier": "",
            "enabled": True,
            "operator": "",
        }
        for i in range(2)
    ]
    return {
        "id": id,
        "ip": ip,
        "enabled": enabled,
        "model": "TG200",
        "ami": {"port": 5038, "user": "u", "secret": "s"},
        "ports": ports,
    }


@pytest.mark.asyncio
async def test_add_and_get():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    c = m.get("a")
    assert c is not None and c.connected and c.host == "10.0.0.1"


@pytest.mark.asyncio
async def test_default_first_enabled():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1", enabled=False))
    await m.add_gateway(_gw("b", "10.0.0.2", enabled=True))
    assert m.default() is m.get("b")


@pytest.mark.asyncio
async def test_resolve_for_number():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1", numbers=["+14105551111", "+14105552222"]))
    res = m.resolve_for_number("4105552222")
    assert res is not None
    client, span = res
    assert client is m.get("a") and span == 3  # slot 1 -> span 3


@pytest.mark.asyncio
async def test_remove_disconnects():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    c = m.get("a")
    await m.remove_gateway("a")
    assert m.get("a") is None and c.disconnected


@pytest.mark.asyncio
async def test_add_survives_connect_failure():
    class BoomClient(FakeClient):
        async def connect(self):
            raise RuntimeError("no socket")

    m = AMIConnectionManager(client_factory=BoomClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))  # must NOT raise
    assert m.get("a") is not None  # entry kept (disconnected)


@pytest.mark.asyncio
async def test_idempotent_replace():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    first = m.get("a")
    await m.add_gateway(_gw("a", "10.0.0.9"))  # same id, new ip
    assert m.get("a").host == "10.0.0.9" and first.disconnected
