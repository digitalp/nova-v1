"""
Phase 3 — HAProxy unit tests.
HA API calls are mocked — no real Home Assistant required.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
from avatar_backend.models.messages import ToolCall
from avatar_backend.services.ha_proxy import HAProxy


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def acl() -> ACLManager:
    return ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="light",  entities="*",
                services=["turn_on", "turn_off", "toggle"]),
        ACLRule(domain="switch", entities=["switch.garden_pump"],
                services=["turn_on", "turn_off"]),
    ]))


@pytest.fixture
def proxy(acl) -> HAProxy:
    return HAProxy(
        ha_url="http://ha.local:8123",
        ha_token="test-token",
        acl=acl,
    )


def _mock_ha_response(status_code: int, json_body=None):
    """Build a mock httpx.Response."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.content = b"[]" if json_body is None else b"{}"
    mock.json.return_value = json_body or []
    mock.text = str(json_body or "")
    return mock


def _tool_call(domain, service, entity_id, service_data=None) -> ToolCall:
    args = {"domain": domain, "service": service, "entity_id": entity_id}
    if service_data:
        args["service_data"] = service_data
    return ToolCall(function_name="call_ha_service", arguments=args)


# ── ACL denial (no HA call made) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acl_denies_unknown_domain(proxy):
    tc = _tool_call("lock", "unlock", "lock.front_door")
    result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "Permission denied" in result.message
    assert result.ha_status_code == 0   # HA was never called


@pytest.mark.asyncio
async def test_acl_denies_unlisted_entity(proxy):
    tc = _tool_call("switch", "turn_on", "switch.living_room_fan")
    result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "switch.living_room_fan" in result.message


@pytest.mark.asyncio
async def test_acl_denies_unlisted_service(proxy):
    tc = _tool_call("light", "set_color", "light.kitchen")
    result = await proxy.execute_tool_call(tc)
    assert result.success is False


# ── Successful HA calls ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_successful_light_turn_on(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_ha_response(200, [])):
        tc = _tool_call("light", "turn_on", "light.kitchen")
        result = await proxy.execute_tool_call(tc)
    assert result.success is True
    assert result.ha_status_code == 200
    assert "light.kitchen" in result.message


@pytest.mark.asyncio
async def test_successful_switch_with_acl(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_ha_response(200, [])):
        tc = _tool_call("switch", "turn_on", "switch.garden_pump")
        result = await proxy.execute_tool_call(tc)
    assert result.success is True


@pytest.mark.asyncio
async def test_service_data_passed_through(proxy):
    """service_data dict should be merged into the HA API payload."""
    captured = {}

    async def fake_post(url, headers, json, **kwargs):
        captured.update(json)
        return _mock_ha_response(200, [])

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        tc = _tool_call("light", "turn_on", "light.kitchen",
                        service_data={"brightness": 200})
        await proxy.execute_tool_call(tc)

    assert captured.get("brightness") == 200
    assert captured.get("entity_id") == "light.kitchen"


# ── HA error responses ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ha_404_entity_not_found(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_ha_response(404, {"message": "Entity not found"})):
        tc = _tool_call("light", "turn_on", "light.nonexistent_room")
        result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert result.ha_status_code == 404
    assert "not found" in result.message.lower()


@pytest.mark.asyncio
async def test_ha_401_bad_token(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_ha_response(401)):
        tc = _tool_call("light", "turn_on", "light.kitchen")
        result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert result.ha_status_code == 401


@pytest.mark.asyncio
async def test_ha_connect_error(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               side_effect=httpx.ConnectError("refused")):
        tc = _tool_call("light", "turn_on", "light.kitchen")
        result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "reach" in result.message.lower()


@pytest.mark.asyncio
async def test_ha_timeout(proxy):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               side_effect=httpx.TimeoutException("timed out")):
        tc = _tool_call("light", "turn_on", "light.kitchen")
        result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "time" in result.message.lower()


# ── Bad tool call inputs ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_function(proxy):
    tc = ToolCall(function_name="delete_all_entities", arguments={})
    result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "Unknown tool" in result.message


@pytest.mark.asyncio
async def test_missing_required_arguments(proxy):
    tc = ToolCall(function_name="call_ha_service",
                  arguments={"domain": "light"})   # missing service and entity_id
    result = await proxy.execute_tool_call(tc)
    assert result.success is False
    assert "missing" in result.message.lower()


# ── No ACL (permissive mode) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_acl_permits_any_call():
    proxy_no_acl = HAProxy(ha_url="http://ha.local:8123",
                           ha_token="token", acl=None)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_ha_response(200, [])):
        tc = _tool_call("lock", "unlock", "lock.front_door")
        result = await proxy_no_acl.execute_tool_call(tc)
    assert result.success is True
