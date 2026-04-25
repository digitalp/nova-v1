"""Heating subsystem — autonomous LLM-driven boiler and TRV control."""
from __future__ import annotations
import asyncio
import traceback


def _format_exc(exc: BaseException) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


import structlog

_LOGGER = structlog.get_logger()


def _is_heating_action_tool(function_name: str, arguments: dict | None) -> bool:
    """Return True when tool_name/args represent a heating write (not a read)."""
    if function_name != "call_ha_service":
        return False
    if not isinstance(arguments, dict):
        return False
    domain = str(arguments.get("domain", "")).strip().lower()
    service = str(arguments.get("service", "")).strip().lower()
    if not domain or not service:
        return False
    return not (domain == "weather" and service == "get_state")


def _load_heating_shadow_prompt() -> str:
    from avatar_backend.runtime_paths import config_dir
    path = config_dir() / "heating_shadow_prompt.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return (
            "You are a heating controller. Read entity states before acting. "
            "If nothing changed, stay silent."
        )


_HEATING_SHADOW_SYSTEM_PROMPT = _load_heating_shadow_prompt()


class HeatingController:
    """Encapsulates Nova's autonomous heating loop.

    Owns the 30-minute evaluation cycle, safety guard (outdoor temp cutoff),
    multi-round agentic LLM loop, and Ollama shadow evaluation.

    Usage::
        controller = HeatingController(ha, llm, system_prompt, announce_fn)
        asyncio.create_task(controller.run_loop(), name="heating_control")
    """

    def __init__(self, ha, llm, system_prompt: str, announce_fn, decision_log=None) -> None:
        self._ha = ha
        self._llm = llm
        self._system_prompt = system_prompt
        self._announce = announce_fn
        self._decision_log = decision_log

    def set_decision_log(self, log) -> None:
        self._decision_log = log

    def _active_llm_fields(self) -> dict[str, str]:
        provider = getattr(self._llm, "provider_name", "unknown")
        model = getattr(self._llm, "model_name", "unknown")
        return {"llm_provider": provider, "llm_model": model, "llm_tag": f"{provider}:{model}"}

    def _local_llm_fields(self) -> dict[str, str]:
        provider = "ollama"
        model = getattr(self._llm, "local_text_model_name", "unknown")
        return {"llm_provider": provider, "llm_model": model, "llm_tag": f"{provider}:{model}"}

    _HEATING_INTERVAL_S = 1800  # evaluate every 30 minutes

    async def run_loop(self) -> None:
        """
        Runs every 30 minutes. Reads room/outdoor temperatures and presence,
        then lets the LLM (with full tool access) decide whether to adjust
        the Hive boiler and winter_mode. Nova is the sole heating controller
        — the schedule-based HA automations have been disabled.
        """
        # Stagger first run by 2 minutes so Nova finishes startup first
        await asyncio.sleep(120)
        while True:
            try:
                await self.evaluate()
            except Exception as exc:
                _LOGGER.warning("heating.eval_error", exc=str(exc))
            await asyncio.sleep(self._HEATING_INTERVAL_S)

    _ALL_TRVS = [
        "climate.living_room_thermostat",
        "climate.living_room_1_thermostat",
        "climate.living_room_2_better_thermostat",
        "climate.main_room_thermo",
        "climate.main_room_thermo_2",
        "climate.bedroom_1_thermostat",
        "climate.bedroom_1_thermo",
        "climate.tse_s_bedroom_thermostat",
        "climate.tse_room_thermostat",
        "climate.hallway",
        "climate.hallway_thermostat",
        "climate.dinning_section_trv",
    ]

    async def _safety_guard(self) -> bool:
        """Pre-flight check before LLM heating evaluation.

        Reads outdoor temp directly from HA (no LLM). If outdoor >= 16 °C,
        enforces heating off + eco TRV setback (13 °C) and returns True so
        _evaluate_heating can skip the LLM loop entirely.
        """
        from avatar_backend.models.messages import ToolCall as _TC

        weather = await self._ha.get_entity_state("weather.met_office_ince_in_makerfield")
        if not weather:
            _LOGGER.warning("heating.guard_outdoor_read_failed")
            return False

        try:
            outdoor_temp = float(weather.get("attributes", {}).get("temperature", 99))
        except (TypeError, ValueError):
            _LOGGER.warning("heating.guard_outdoor_parse_failed", state=str(weather)[:120])
            return False

        if outdoor_temp < 16.0:
            return False  # within range where LLM should decide

        _LOGGER.info(
            "heating.safety_guard_triggered",
            outdoor_temp=outdoor_temp,
            action="force_off_eco_setback",
        )

        async def _act(args: dict) -> None:
            await self._ha.execute_tool_call(_TC(function_name="call_ha_service", arguments=args))

        # Turn off boiler and winter_mode
        await _act({"domain": "input_boolean", "service": "turn_off",
                    "entity_id": "input_boolean.winter_mode"})
        await _act({"domain": "climate", "service": "set_hvac_mode",
                    "entity_id": "climate.hive_receiver_climate",
                    "service_data": {"hvac_mode": "off"}})

        # Eco setback — all TRVs to 13 °C
        for trv in self._ALL_TRVS:
            await _act({"domain": "climate", "service": "set_temperature",
                        "entity_id": trv, "service_data": {"temperature": 13}})

        return True


    async def evaluate(self) -> None:
        """
        Runs a full agentic loop (LLM + tool execution) to evaluate and
        adjust heating. The system prompt contains the decision rules.
        """
        # Pre-flight: if outdoor >= 16°C enforce off + eco setback without LLM
        if await self._safety_guard():
            _LOGGER.info("heating.guard_handled", reason="outdoor>=16C")
            return

        import datetime as _dt
        now_str = _dt.datetime.now().strftime("%A, %d %B %Y %H:%M")
        month = _dt.datetime.now().month
        season = "spring/summer" if 4 <= month <= 9 else "autumn/winter"

        task_msg = (
            f"[Autonomous heating evaluation — {now_str}, {season}] "
            "Read all room temperature sensors, the outdoor temperature, and current presence. "
            "Then apply the heating decision rules from your system prompt and take action if needed. "
            "Be concise — one sentence announcement only if something changed, silent otherwise."
        )

        from avatar_backend.config import get_settings as _get_settings
        _hlp = (_get_settings().heating_llm_provider or "gemini").strip().lower()
        _use_ollama_primary = _hlp == "ollama" and hasattr(self._llm, "chat_local")
        _heating_fields = self._local_llm_fields() if _use_ollama_primary else self._active_llm_fields()

        # When Ollama is the primary, use the focused heating prompt (not the full 63KB system prompt)
        if _use_ollama_primary:
            messages = [
                {"role": "system", "content": _HEATING_SHADOW_SYSTEM_PROMPT},
                {"role": "user",   "content": task_msg},
            ]
        else:
            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user",   "content": task_msg},
            ]

        _MAX_ROUNDS = 6
        _LOGGER.info("heating.eval_start", provider=_hlp)
        if self._decision_log:
            self._decision_log.record(
                "heating_eval_start",
                season=season,
                time=now_str,
                provider=_hlp,
                **_heating_fields,
            )

        # Shadow run: only when Gemini is primary and shadow is enabled
        _shadow_enabled = _get_settings().heating_shadow_enabled
        shadow_calls: list[dict] = []
        if not _use_ollama_primary and _shadow_enabled:
            try:
                shadow_calls = await asyncio.wait_for(
                    self._run_shadow(messages, season=season, now_str=now_str),
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning("heating.shadow_eval_timeout", timeout_s=120.0)

        all_tool_calls: list[str] = []
        performed_action = False

        for round_num in range(_MAX_ROUNDS):
            if _use_ollama_primary:
                text, tool_calls = await self._llm.chat_local(messages, use_tools=True)
            else:
                text, tool_calls = await self._llm.chat(messages, use_tools=True)

            if not tool_calls:
                # LLM gave a final text response
                if (
                    performed_action
                    and text
                    and text.strip()
                    and "nothing changed" not in text.lower()
                    and "no change" not in text.lower()
                ):
                    _LOGGER.info("heating.eval_announce", message=text[:120])
                    # Suppress announcements during quiet hours (23:00-07:00)
                    import datetime as _dt
                    _hour = _dt.datetime.now().hour
                    _quiet = _hour >= 23 or _hour < 7
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_action",
                            message=text.strip()[:300],
                            tool_calls=all_tool_calls,
                            **_heating_fields,
                        )
                    if not _quiet:
                        await self._announce(text.strip(), "normal")
                    else:
                        _LOGGER.info("heating.eval_quiet_hours_suppressed")
                else:
                    _LOGGER.info("heating.eval_silent")
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_eval_silent",
                            reason=(
                                text.strip()[:200]
                                if (text and performed_action)
                                else "no heating action executed"
                            ),
                            tool_calls=all_tool_calls,
                            performed_action=performed_action,
                            **_heating_fields,
                        )
                break

            # Build assistant turn in OpenAI wire format
            raw_tcs = [
                {"id": f"htool_{i}", "type": "function",
                 "function": {"name": tc.function_name, "arguments": tc.arguments}}
                for i, tc in enumerate(tool_calls)
            ]
            messages.append({"role": "assistant", "content": text or "", "tool_calls": raw_tcs})

            # Execute each tool call
            for i, tc in enumerate(tool_calls):
                result = await self._ha.execute_tool_call(tc)
                performed_action = performed_action or _is_heating_action_tool(
                    tc.function_name,
                    tc.arguments,
                )
                summary = f"{tc.function_name}({tc.arguments}) → {(result.message or '')[:80]}"
                all_tool_calls.append(summary)
                _LOGGER.info(
                    "heating.tool_call",
                    tool=tc.function_name,
                    args=tc.arguments,
                    success=result.success,
                    result=(result.message or "")[:120],
                )
                if self._decision_log:
                    self._decision_log.record(
                        "heating_tool_call",
                        tool=tc.function_name,
                        args={k: str(v)[:80] for k, v in tc.arguments.items()},
                        success=result.success,
                        result=(result.message or "")[:200],
                        **_heating_fields,
                    )
                messages.append({
                    "role":         "tool",
                    "tool_call_id": f"htool_{i}",
                    "content":      result.message or "",
                })

            if round_num == _MAX_ROUNDS - 1:
                _LOGGER.warning("heating.eval_max_rounds")
                if self._decision_log:
                    self._decision_log.record("heating_eval_max_rounds", rounds=_MAX_ROUNDS)
                break

        _LOGGER.info("heating.eval_done")
        self._log_shadow_comparison(
            shadow_calls=shadow_calls,
            primary_tool_calls=all_tool_calls,
            primary_performed_action=performed_action,
        )

    async def _run_shadow(
        self,
        messages: list[dict],
        *,
        season: str,
        now_str: str,
        shadow_only: bool = False,
    ) -> list[dict]:
        """
        Full multi-round local shadow evaluation using Ollama.

        Read tools (get_entity_state, get_entities) execute for real so Ollama
        receives actual sensor data.  Write tools (call_ha_service) are
        intercepted — logged but never applied to HA.

        Returns a list of per-tool-call records for comparison with the primary.
        """
        if not hasattr(self._llm, "chat_local"):
            return []

        _MAX_SHADOW_ROUNDS = 6
        # Use the compact heating-specific system prompt for Ollama — the full Nova
        # system prompt is ~15k tokens and makes inference very slow (causes timeouts).
        shadow_messages = list(messages)
        if shadow_messages and shadow_messages[0].get("role") == "system":
            shadow_messages = [
                {"role": "system", "content": _HEATING_SHADOW_SYSTEM_PROMPT}
            ] + shadow_messages[1:]
        shadow_records: list[dict] = []

        _LOGGER.info("heating.shadow_eval_start", season=season, shadow_only=shadow_only)
        if self._decision_log:
            self._decision_log.record(
                "heating_shadow_eval_start",
                season=season,
                time=now_str,
                shadow_only=shadow_only,
                **self._local_llm_fields(),
            )

        try:
            for round_num in range(_MAX_SHADOW_ROUNDS):
                text, tool_calls = await self._llm.chat_local(shadow_messages, use_tools=True)

                if not tool_calls:
                    reason = (text or "").strip()[:200] or "no action suggested"
                    _LOGGER.info("heating.shadow_round_silent", round=round_num, reason=reason)
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_shadow_round_silent",
                            round=round_num,
                            reason=reason,
                            **self._local_llm_fields(),
                        )
                    break

                raw_tcs = [
                    {
                        "id": f"shtool_{round_num}_{i}",
                        "type": "function",
                        "function": {"name": tc.function_name, "arguments": tc.arguments},
                    }
                    for i, tc in enumerate(tool_calls)
                ]
                shadow_messages.append({
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": raw_tcs,
                })

                for i, tc in enumerate(tool_calls):
                    is_write = _is_heating_action_tool(tc.function_name, tc.arguments)
                    rec: dict = {
                        "round": round_num,
                        "tool": tc.function_name,
                        "args": {k: str(v)[:80] for k, v in tc.arguments.items()},
                        "is_write": is_write,
                    }

                    if is_write:
                        # Intercept: log intent but never apply to HA
                        tool_result_content = "Done (shadow — not executed)"
                        rec["result"] = tool_result_content
                        rec["executed"] = False
                        _LOGGER.info(
                            "heating.shadow_tool_intercepted",
                            round=round_num,
                            tool=tc.function_name,
                            args=tc.arguments,
                        )
                    else:
                        # Read tools: execute for real so Ollama gets live data
                        try:
                            result = await self._ha.execute_tool_call(tc)
                            tool_result_content = result.message or ""
                            rec["result"] = tool_result_content[:200]
                            rec["executed"] = True
                        except Exception as exc:
                            tool_result_content = f"Error: {_format_exc(exc)}"
                            rec["result"] = tool_result_content[:200]
                            rec["executed"] = False

                    shadow_records.append(rec)
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_shadow_tool_call",
                            round=round_num,
                            tool=tc.function_name,
                            args=rec["args"],
                            is_write=is_write,
                            result=rec["result"],
                            executed=rec["executed"],
                            **self._local_llm_fields(),
                        )

                    shadow_messages.append({
                        "role": "tool",
                        "tool_call_id": f"shtool_{round_num}_{i}",
                        "content": tool_result_content,
                    })

                if round_num == _MAX_SHADOW_ROUNDS - 1:
                    _LOGGER.warning("heating.shadow_max_rounds", rounds=_MAX_SHADOW_ROUNDS)
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_shadow_max_rounds",
                            rounds=_MAX_SHADOW_ROUNDS,
                            **self._local_llm_fields(),
                        )

        except Exception as exc:
            formatted_exc = _format_exc(exc)
            _LOGGER.warning("heating.shadow_eval_failed", exc=formatted_exc[:200])
            if self._decision_log:
                self._decision_log.record(
                    "heating_shadow_eval_error",
                    reason=formatted_exc[:200],
                    **self._local_llm_fields(),
                )

        return shadow_records

    def _log_shadow_comparison(
        self,
        *,
        shadow_calls: list[dict],
        primary_tool_calls: list[str],
        primary_performed_action: bool,
    ) -> None:
        """Compare shadow (Ollama) vs primary (Gemini) and log the diff."""
        shadow_writes = [r for r in shadow_calls if r["is_write"]]
        shadow_acted = bool(shadow_writes)

        if shadow_acted and primary_performed_action:
            agreement = "both_acted"
        elif not shadow_acted and not primary_performed_action:
            agreement = "both_silent"
        elif shadow_acted and not primary_performed_action:
            agreement = "shadow_only"
        else:
            agreement = "primary_only"

        # Extract entity_ids from shadow writes
        shadow_entities = sorted({
            r["args"].get("entity_id", "")
            for r in shadow_writes
            if r["args"].get("entity_id")
        })

        # Extract entity_ids from primary summaries (format: "call_ha_service({...}) → ...")
        primary_entities: list[str] = []
        for summary in primary_tool_calls:
            if "entity_id" in summary:
                import re as _re
                m = _re.search(r"'entity_id':\s*'([^']+)'", summary)
                if m:
                    primary_entities.append(m.group(1))
        primary_entities = sorted(set(primary_entities))

        entity_overlap = sorted(set(shadow_entities) & set(primary_entities))
        entity_shadow_only = sorted(set(shadow_entities) - set(primary_entities))
        entity_primary_only = sorted(set(primary_entities) - set(shadow_entities))

        _LOGGER.info(
            "heating.shadow_comparison",
            agreement=agreement,
            shadow_writes=len(shadow_writes),
            primary_writes=len(primary_tool_calls),
        )
        if self._decision_log:
            self._decision_log.record(
                "heating_shadow_comparison",
                agreement=agreement,
                shadow_writes=[f"{r['tool']}({r['args']})" for r in shadow_writes],
                primary_calls=primary_tool_calls[:12],
                shadow_entities=shadow_entities,
                primary_entities=primary_entities,
                entity_overlap=entity_overlap,
                entity_shadow_only=entity_shadow_only,
                entity_primary_only=entity_primary_only,
                **self._local_llm_fields(),
            )

    async def run_force(
        self,
        *,
        scenario: str = "winter",
    ) -> list[dict]:
        """
        Admin-triggered shadow-only evaluation.  Never touches HA writes.
        Use scenario='winter' to inject a cold-weather test note so Ollama
        reasons about a heating-on scenario even in summer.
        """
        import datetime as _dt
        now_str = _dt.datetime.now().strftime("%A, %d %B %Y %H:%M")

        # Scenario context sets the season and outdoor temperature only —
        # room temperatures are intentionally omitted so Ollama must read
        # the actual sensors via get_entity_state rather than short-circuiting.
        scenario_ctx = {
            "winter": {
                "season": "autumn/winter",
                "hint": "It is a cold winter morning. Outdoor temperature is 3 °C.",
            },
            "spring": {
                "season": "spring/summer",
                "hint": "It is a warm spring day. Outdoor temperature is 17 °C.",
            },
        }
        ctx = scenario_ctx.get(scenario, scenario_ctx["winter"])
        season = ctx["season"]

        task_msg = (
            f"[Shadow-only heating evaluation — {now_str}, {season}] "
            f"{ctx['hint']} "
            "Read all room temperature sensors and current presence using get_entity_state, "
            "then apply the heating decision rules from your system prompt and state what "
            "actions you would take. Be concise."
        )
        messages = [
            {"role": "system", "content": _HEATING_SHADOW_SYSTEM_PROMPT},
            {"role": "user",   "content": task_msg},
        ]
        return await self._run_shadow(
            messages, season=season, now_str=now_str, shadow_only=True
        )

