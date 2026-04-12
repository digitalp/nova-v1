"""
Standalone prompt sync — same logic as POST /admin/sync-prompt but no HTTP auth.
Run with: /opt/avatar-server/.venv/bin/python3 /tmp/do_sync.py
"""
import asyncio, os, sys, shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/opt/avatar-server')
for line in open('/opt/avatar-server/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

from avatar_backend.config import get_settings
from avatar_backend.services.prompt_bootstrap import extract_known_entity_ids, summarise_new_entities
from avatar_backend.services.llm_service import LLMService
import httpx

PROMPT_FILE = Path('/opt/avatar-server/config/system_prompt.txt')
BACKUP_FILE = Path('/opt/avatar-server/config/system_prompt.txt.bak')

async def run():
    settings = get_settings()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching HA states...")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f'{settings.ha_url}/api/states',
            headers={'Authorization': f'Bearer {settings.ha_token}'},
        )
        resp.raise_for_status()
        all_states = resp.json()

    current_prompt = PROMPT_FILE.read_text()
    known = extract_known_entity_ids(current_prompt)
    new_summary = summarise_new_entities(all_states, known)

    print(f"Total HA entities : {len(all_states)}")
    print(f"Known in prompt   : {len(known)}")

    if not new_summary:
        print("✓ Prompt is already up to date — no new entities found.")
        return

    new_count = new_summary.count('\n  ')
    print(f"New entities found: {new_count}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Calling LLM to integrate...")

    llm = LLMService()
    integration_request = (
        "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
        "Here is the current system prompt:\n```\n" + current_prompt + "\n```\n\n"
        "The following new Home Assistant entities have been discovered:\n\n"
        + new_summary + "\n\n"
        "Instructions:\n"
        "- Add these entities to appropriate existing sections.\n"
        "- Skip clear infrastructure noise (connectivity sensors, cloud connection sensors).\n"
        "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
        "- Return ONLY the complete updated system prompt — no explanation, no markdown fences."
    )

    updated_prompt = await llm.generate_text(integration_request, timeout_s=240.0)

    if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
        print("✗ LLM returned unexpectedly short response — aborting.")
        sys.exit(1)
    if len(updated_prompt) > len(current_prompt) * 3:
        print("✗ LLM returned unexpectedly long response — aborting.")
        sys.exit(1)

    updated_prompt = "".join(c for c in updated_prompt if c >= " " or c in "\n\r\t")

    # Backup and save
    shutil.copy(PROMPT_FILE, BACKUP_FILE)
    PROMPT_FILE.write_text(updated_prompt)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Done.")
    print(f"  Backup : {BACKUP_FILE} ({BACKUP_FILE.stat().st_size} bytes)")
    print(f"  Updated: {PROMPT_FILE} ({PROMPT_FILE.stat().st_size} bytes)")
    print(f"  Lines  : {len(current_prompt.splitlines())} → {len(updated_prompt.splitlines())}")

asyncio.run(run())
