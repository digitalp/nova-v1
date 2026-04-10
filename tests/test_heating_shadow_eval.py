from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services.ha_proxy import ToolCall
from avatar_backend.services.proactive_service import ProactiveService, _shape_heating_announcement


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
        fast_local_text_model_name="qwen2.5:7b",
        chat_local_fast_resilient=AsyncMock(return_value=(
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

    llm.chat_local_fast_resilient.assert_awaited_once()
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
        fast_local_text_model_name="qwen2.5:7b",
        chat_local_fast_resilient=AsyncMock(return_value=("No change needed right now.", [])),
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
        fast_local_text_model_name="qwen2.5:7b",
        chat_local_fast_resilient=AsyncMock(side_effect=TimeoutError()),
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


@pytest.mark.asyncio
async def test_heating_shadow_uses_legacy_local_path_when_fast_path_missing():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        chat_local=AsyncMock(return_value=("No action.", [])),
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

    await service._run_heating_shadow(
        [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "evaluate heating"}],
        season="autumn/winter",
        now_str="Friday, 10 April 2026 20:00",
    )

    llm.chat_local.assert_awaited_once()


def test_shape_heating_announcement_strips_report_style_boilerplate():
    raw = (
        "Based on the provided sensor and device status information, here is a summary of the key points:\n\n"
        "### Temperature & Humidity\n"
        "The dining section temperature is quite high at 32.6°C and the living room is also warm at 30.4°C. "
        "Please consider lowering the thermostats for comfort."
    )

    shaped = _shape_heating_announcement(raw)

    assert "summary of the key points" not in shaped.lower()
    assert "temperature & humidity" not in shaped.lower()
    assert shaped == (
        "The dining section temperature is quite high at 32.6 degrees Celsius and the living room is also warm at 30.4 degrees Celsius. "
        "Please consider lowering the thermostats for comfort."
    )


@pytest.mark.asyncio
async def test_evaluate_heating_announces_shaped_spoken_text():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        fast_local_text_model_name="qwen2.5:7b",
        chat=AsyncMock(
            return_value=(
                "Based on the provided sensor and device status information, here is a summary of the key points:\n\n"
                "### Temperature & Humidity\n"
                "The dining section temperature is quite high at 32.6°C and the living room is also warm at 30.4°C. "
                "Please consider lowering the thermostats for comfort.",
                [],
            )
        ),
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
    service._run_heating_shadow = AsyncMock()
    log = _DecisionLog()
    service.set_decision_log(log)

    await service._evaluate_heating()

    service._announce.assert_awaited_once_with(
        "The dining section temperature is quite high at 32.6 degrees Celsius and the living room is also warm at 30.4 degrees Celsius. "
        "Please consider lowering the thermostats for comfort.",
        "normal",
    )
    action_rows = [payload for kind, payload in log.records if kind == "heating_action"]
    assert action_rows
    assert action_rows[0]["message"].startswith("The dining section temperature is quite high")
    assert "summary of the key points" in action_rows[0]["raw_message"].lower()
