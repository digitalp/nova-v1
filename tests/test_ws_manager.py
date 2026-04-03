"""
Phase 4 — WebSocket ConnectionManager tests.
Uses mock WebSocket objects to avoid requiring a running server.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from avatar_backend.services.ws_manager import ConnectionManager


def _mock_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect_calls_accept():
    mgr = ConnectionManager()
    ws = _mock_ws()
    await mgr.connect(ws)
    ws.accept.assert_called_once()


@pytest.mark.asyncio
async def test_connection_count_tracks_connections():
    mgr = ConnectionManager()
    ws1, ws2 = _mock_ws(), _mock_ws()
    await mgr.connect(ws1)
    await mgr.connect(ws2)
    assert mgr.connection_count == 2


@pytest.mark.asyncio
async def test_disconnect_removes_connection():
    mgr = ConnectionManager()
    ws = _mock_ws()
    await mgr.connect(ws)
    await mgr.disconnect(ws)
    assert mgr.connection_count == 0


@pytest.mark.asyncio
async def test_disconnect_unknown_is_safe():
    mgr = ConnectionManager()
    ws = _mock_ws()
    await mgr.disconnect(ws)  # never connected — should not raise


@pytest.mark.asyncio
async def test_broadcast_sends_to_all():
    mgr = ConnectionManager()
    ws1, ws2 = _mock_ws(), _mock_ws()
    await mgr.connect(ws1)
    await mgr.connect(ws2)

    await mgr.broadcast_json({"type": "avatar_state", "state": "thinking"})

    ws1.send_text.assert_called_once()
    ws2.send_text.assert_called_once()

    payload = json.loads(ws1.send_text.call_args[0][0])
    assert payload["state"] == "thinking"


@pytest.mark.asyncio
async def test_broadcast_empty_registry_is_safe():
    mgr = ConnectionManager()
    await mgr.broadcast_json({"type": "test"})  # no clients — should not raise


@pytest.mark.asyncio
async def test_broadcast_prunes_dead_connections():
    """Dead connections (send_text raises) should be silently removed."""
    mgr = ConnectionManager()
    dead_ws = _mock_ws()
    dead_ws.send_text.side_effect = RuntimeError("connection closed")
    good_ws = _mock_ws()

    await mgr.connect(dead_ws)
    await mgr.connect(good_ws)

    await mgr.broadcast_json({"type": "ping"})

    # Dead connection should have been pruned
    assert mgr.connection_count == 1
    good_ws.send_text.assert_called_once()
