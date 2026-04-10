from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services.ha_proxy import ToolCall
from avatar_backend.services.proactive_service import ProactiveService


class _DecisionLog:
    def __init__(self):
        self.records = []

    def record(self, kind, **kwargs):
        self.records.append((kind, kwargs))


@pytest.mark.asyncio
async def test_heating_shadow_logs_tool_calls_without_execution():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        chat_local=AsyncMock(return_value=(
            "I would raise the heating.",
            [ToolCall(function_name="call_ha_service", arguments={"domain": "climate", "service": "set_temperature"})],
        )),
        chat=AsyncMock(),
    )
    service = ProactiveService(
        ha_url="http://ha.local",
        ha_token="token",
        ha_proxy=SimpleNamespace(),
        llm_service=llm,
        motion_clip_service=SimpleNamespace(),
        announce_fn=AsyncMock(),
        system_prompt="system prompt",
    )
    log = _DecisionLog()
    service.set_decision_log(log)

    await service._run_heating_shadow(
        [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "evaluate heating"}],
        season="autumn/winter",
        now_str="Friday, 10 April 2026 20:00",
    )

    llm.chat_local.assert_awaited_once()
    kinds = [kind for kind, _ in log.records]
    assert "heating_shadow_eval_start" in kinds
    assert "heating_shadow_tool_call" in kinds
    assert "heating_shadow_action" in kinds
    assert llm.chat.await_count == 0


@pytest.mark.asyncio
async def test_heating_shadow_logs_silence_when_local_model_suggests_no_change():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        chat_local=AsyncMock(return_value=("No change needed right now.", [])),
    )
    service = ProactiveService(
        ha_url="http://ha.local",
        ha_token="token",
        ha_proxy=SimpleNamespace(),
        llm_service=llm,
        motion_clip_service=SimpleNamespace(),
        announce_fn=AsyncMock(),
        system_prompt="system prompt",
    )
    log = _DecisionLog()
    service.set_decision_log(log)

    await service._run_heating_shadow(
        [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "evaluate heating"}],
        season="autumn/winter",
        now_str="Friday, 10 April 2026 20:00",
    )

    kinds = [kind for kind, _ in log.records]
    assert "heating_shadow_eval_start" in kinds
    assert "heating_shadow_eval_silent" in kinds


@pytest.mark.asyncio
async def test_heating_shadow_logs_typed_error_reason():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        chat_local=AsyncMock(side_effect=TimeoutError()),
    )
    service = ProactiveService(
        ha_url="http://ha.local",
        ha_token="token",
        ha_proxy=SimpleNamespace(),
        llm_service=llm,
        motion_clip_service=SimpleNamespace(),
        announce_fn=AsyncMock(),
        system_prompt="system prompt",
    )
    log = _DecisionLog()
    service.set_decision_log(log)

    await service._run_heating_shadow(
        [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "evaluate heating"}],
        season="autumn/winter",
        now_str="Friday, 10 April 2026 20:00",
    )

    error_rows = [payload for kind, payload in log.records if kind == "heating_shadow_eval_error"]
    assert error_rows
    assert error_rows[0]["reason"] == "TimeoutError"
