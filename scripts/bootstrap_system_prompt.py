#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from avatar_backend.services.prompt_bootstrap import (  # noqa: E402
    build_home_runtime_config,
    fetch_ha_states,
    generate_prompt,
    parse_notes,
    parse_other_members,
    parse_primary_users,
    parse_vehicle_profiles,
)
from avatar_backend.services.home_runtime import write_home_runtime_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Nova's initial system prompt from Home Assistant.")
    parser.add_argument("--template", required=True, help="Path to system_prompt_template.txt")
    parser.add_argument("--output", required=True, help="Path to write system_prompt.txt")
    parser.add_argument("--runtime-output", default="", help="Optional path to write home_runtime.json")
    parser.add_argument("--ha-url", default="", help="Home Assistant base URL")
    parser.add_argument("--ha-token", default="", help="Home Assistant long-lived token")
    parser.add_argument("--address", required=True, help="Home label or address")
    parser.add_argument("--timezone", required=True, help="Local timezone name")
    parser.add_argument("--default-user", required=True, help="Fallback user name")
    parser.add_argument("--primary-users", default="", help="Comma-separated primary household members")
    parser.add_argument(
        "--other-members",
        default="",
        help="Semicolon-separated members in the form 'Name:role' or 'Name:role,details'",
    )
    parser.add_argument(
        "--vehicles",
        default="",
        help="Semicolon-separated vehicles in the form 'Owner:description'",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Semicolon-separated stable household notes",
    )
    args = parser.parse_args()

    template_path = Path(args.template)
    output_path = Path(args.output)
    template_text = template_path.read_text()

    household = parse_primary_users(args.primary_users, args.default_user)
    household.extend(parse_other_members(args.other_members))
    vehicles = parse_vehicle_profiles(args.vehicles)
    notes = parse_notes(args.notes)

    states: list[dict] | None = None
    source_label = "Manual installer inputs only"
    if args.ha_url and args.ha_token:
        try:
            states = fetch_ha_states(args.ha_url, args.ha_token)
            source_label = f"Home Assistant discovery from {args.ha_url.rstrip('/')}/api/states"
            print(f"Fetched {len(states)} Home Assistant entities for prompt bootstrap.")
        except RuntimeError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            print("Continuing with installer-provided context only.", file=sys.stderr)

    generated = generate_prompt(
        template_text=template_text,
        address=args.address,
        timezone_name=args.timezone,
        household=household,
        vehicles=vehicles,
        extra_notes=notes,
        states=states,
        source_label=source_label,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generated)
    print(f"Wrote personalised system prompt to {output_path}")

    if args.runtime_output:
        runtime_config = build_home_runtime_config(states or [], vehicles, notes)
        write_home_runtime_config(runtime_config, Path(args.runtime_output))
        print(f"Wrote runtime HA mappings to {args.runtime_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
