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

@pytest.mark.asyncio
async def test_evaluate_heating_stays_silent_for_read_only_summary():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
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

    llm.chat.assert_awaited_once()
    prompt_text = llm.chat.await_args.args[0][1]["content"]
    assert "Autonomous heating evaluation" in prompt_text
    assert "one sentence announcement only if something changed" in prompt_text
    service._announce.assert_not_awaited()
    silent_rows = [payload for kind, payload in log.records if kind == "heating_eval_silent"]
    assert silent_rows
    assert silent_rows[0]["reason"] == "no heating action executed"
    assert silent_rows[0]["performed_action"] is False


@pytest.mark.asyncio
async def test_evaluate_heating_announces_only_after_real_action():
    llm = SimpleNamespace(
        provider_name="google",
        model_name="gemini-2.5-flash",
        local_text_model_name="mistral-nemo:12b",
        chat=AsyncMock(
            side_effect=[
                (
                    "",
                    [
                        ToolCall(
                            function_name="call_ha_service",
                            arguments={
                                "domain": "climate",
                                "service": "set_temperature",
                                "entity_id": "climate.living_room_1_thermostat",
                            },
                        )
                    ],
                ),
                ("Heating adjusted to maintain comfort in the living room.", []),
            ]
        ),
    )
    ha_proxy = SimpleNamespace(
        execute_tool_call=AsyncMock(return_value=SimpleNamespace(success=True, message="ok"))
    )
    service = ProactiveService(
        ha_url="http://ha.local",
        ha_token="token",
        ha_proxy=ha_proxy,
        llm_service=llm,
        motion_clip_service=SimpleNamespace(),
        announce_fn=AsyncMock(),
        system_prompt="system prompt",
    )
    service._run_heating_shadow = AsyncMock()
    log = _DecisionLog()
    service.set_decision_log(log)

    await service._evaluate_heating()

    ha_proxy.execute_tool_call.assert_awaited_once()
    service._announce.assert_awaited_once_with(
        "Heating adjusted to maintain comfort in the living room.",
        "normal",
    )
    action_rows = [payload for kind, payload in log.records if kind == "heating_action"]
    assert action_rows
    assert action_rows[0]["message"] == "Heating adjusted to maintain comfort in the living room."
