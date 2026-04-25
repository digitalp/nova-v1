"""HA tool schemas for Ollama/OpenAI and Anthropic wire formats."""
from __future__ import annotations

# ── Tool schemas (OpenAI/Ollama format) ───────────────────────────────────────

HA_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_entities",
            "description": (
                "List available Home Assistant entities for a domain with their "
                "current state. Always call this FIRST if you are unsure of the "
                "exact entity_id before calling call_ha_service."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Entity domain to list. Examples: light, switch, "
                            "media_player, climate, cover, fan, sensor, "
                            "binary_sensor, lock, automation, input_boolean."
                        ),
                    },
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_state",
            "description": (
                "Get the current state and value of a specific Home Assistant entity. "
                "Use this to answer questions like 'what is the power consumption', "
                "'is the light on', 'what is the temperature', etc. "
                "Use get_entities first if you don't know the exact entity_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The full entity ID, e.g. sensor.total_power, light.kitchen.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_ha_service",
            "description": (
                "Control a Home Assistant device by calling a service (turn on/off, lock, unlock, etc). "
                "Use get_entities first if you are unsure of the entity_id. "
                "NEVER use this to read sensor values - use get_entity_state instead. "
                "NEVER call tts or media_player speak services - your text responses are automatically spoken."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain":       {"type": "string"},
                    "service":      {"type": "string"},
                    "entity_id":    {"type": "string"},
                    "service_data": {"type": "object"},
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_camera",
            "description": (
                "Capture a snapshot from a Home Assistant camera and describe what it sees. "
                "Use get_entities('camera') first if you don't know the entity_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The camera entity ID, e.g. camera.front_door.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": (
                "Search for and play music on a speaker. Searches Music Assistant "
                "for the artist/song/album, then plays the first result on the specified speaker. "
                "Use this instead of call_ha_service for music playback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for - artist name, song title, or album.",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "The media_player entity ID to play on, e.g. media_player.living_room_sonos.",
                    },
                },
                "required": ["query", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_chore",
            "description": (
                "Record that a person completed a chore and award them points on the family scoreboard. "
                "Use when someone says they did a task like emptied the bin, made their bed, tidied their room, "
                "said prayers, etc. Valid task_ids: morning_prayer, make_bed, meal_prayer, empty_toilet_bin, "
                "tidy_bedroom, empty_kitchen_bin, tidy_living_room, wipe_kitchen, "
                "clear_table, hoover_living_room, take_recycling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task identifier, e.g. 'empty_kitchen_bin'.",
                    },
                    "person": {
                        "type": "string",
                        "description": "The person's name, e.g. 'penn' or 'tangu'.",
                    },
                },
                "required": ["task_id", "person"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scoreboard",
            "description": (
                "Look up the family chore scoreboard. Use whenever someone asks about chores: "
                "scores, rankings, who is winning, how many chores done today or this week, "
                "recent activity, or any scoreboard question. "
                "Pass a period of today, week, or recent depending on what they asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "week", "recent"],
                        "description": "today=chores today, week=weekly scores, recent=last 10 logs",
                    },
                },
                "required": ["period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_enrolled_devices",
            "description": (
                "List all enrolled children's Android devices managed by Headwind MDM. "
                "Returns device number, name, online status, and last seen time. "
                "Call this first to find the device_number before blocking apps or sending messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_parental_status",
            "description": (
                "Check whether the Headwind parental management backend is reachable. "
                "Use this before deeper device-management actions if parental controls seem unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_household_at",
            "description": (
                "Simulate what household rules would do at a specific time and day, without enforcing. "
                "Use when asked what happens at a given time, e.g. will Jason be locked at 9 PM Saturday."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "Time in HH:MM (24h), e.g. 21:30."},
                    "day":  {"type": "string", "description": "Day name e.g. monday, saturday. Defaults to today."},
                },
                "required": ["time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_household_forecast",
            "description": (
                "Show what is coming up for the household in the next few hours: "
                "upcoming bedtimes, homework gate windows, chore check-ins, and current device states. "
                "Use when asked what will happen next, what is scheduled, or for a household overview."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bedtime_status",
            "description": (
                "Get tonight's bedtime for a household member. "
                "Returns bedtime time, whether it is a school night, and current device state. "
                "Use when asked about bedtimes, screen time, or device curfews."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {
                        "type": "string",
                        "description": "Household member ID (e.g. jason, joel, miya).",
                    },
                },
                "required": ["person_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_location",
            "description": (
                "Get the latest known location for a household member or managed device. "
                "Provide person_id (e.g. jason, joel, miya) to look up by name — no need to "
                "call get_enrolled_devices first. Returns a human-readable address when available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {
                        "type": "string",
                        "description": "The person's ID from the household roster (e.g. jason, joel, miya).",
                    },
                    "device_number": {
                        "type": "string",
                        "description": "Direct MDM device number — only needed if person_id is unknown.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_apps",
            "description": (
                "Search Headwind's Android app catalog to find package names and whether an app is installable or allow-only. "
                "Use this before deploy_app if you are unsure of the exact package."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "App name or package fragment, e.g. 'YouTube', 'WhatsApp', or 'roblox'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "block_app",
            "description": (
                "Block (hide/disable) an Android app on a child's device. "
                "The app is removed from the home screen and cannot be opened. "
                "Use get_enrolled_devices first to find the device_number. "
                "Common packages: TikTok=com.zhiliaoapp.musically, Instagram=com.instagram.android, "
                "WhatsApp=com.whatsapp, Snapchat=com.snapchat.android, YouTube=com.google.android.youtube, "
                "Facebook=com.facebook.katana, X/Twitter=com.twitter.android, Roblox=com.roblox.client."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number/ID from get_enrolled_devices (often the child name, e.g. 'Jason').",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name, e.g. 'com.zhiliaoapp.musically'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unblock_app",
            "description": (
                "Unblock (re-enable) a previously blocked Android app on a child's device. "
                "Use get_enrolled_devices first to find the device_number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name to unblock, e.g. 'com.google.android.youtube'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_app",
            "description": (
                "Deploy or allow an Android app on a child's device using Headwind's app catalog. "
                "For installable apps, Headwind marks them for install. "
                "For system or allow-only apps, Nova can only allow them, not silently install them. "
                "Use get_enrolled_devices and optionally search_apps first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name, e.g. 'com.whatsapp'.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional display name for clearer confirmations, e.g. 'WhatsApp'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_homework_gate",
            "description": "Check whether a child has completed their required tasks and whether their device is currently locked or unlocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The child's ID (e.g. joel, jason, miya)"},
                },
                "required": ["person_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "request_exception",
            "description": (
                "Submit a parental exception request — extra screen time, a bedtime extension, "
                "or temporary access to a blocked resource. The request is queued for a parent "
                "to approve or deny in the admin panel. Use when a child asks for something "
                "that needs parental approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Name of the person requesting (e.g. Joel)"},
                    "resource": {"type": "string", "description": "What they want (e.g. Xbox, iPad, YouTube)"},
                    "reason": {"type": "string", "description": "Their reason for the exception"},
                    "duration_minutes": {"type": "integer", "description": "How long they are asking for (minutes)", "default": 30}
                },
                "required": ["subject", "reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_device_message",
            "description": (
                "Send a full-screen push notification to a child's Android device. "
                "Good for: 'come for dinner', 'homework time', 'phone off now', 'bedtime'. "
                "Use get_enrolled_devices first to find the device_number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to display on the device.",
                    },
                },
                "required": ["device_number", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_parental_configurations",
            "description": (
                "List Headwind parental configurations. Useful for enrollment and understanding which configuration a device belongs to."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_enrollment_link",
            "description": (
                "Get the enrollment URL for a Headwind configuration so a parent can enroll a device. "
                "This returns the enroll link and QR key as text, not the QR image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "config_id": {
                        "type": "integer",
                        "description": "Headwind configuration id, e.g. 2.",
                    },
                },
                "required": ["config_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deduct_points",
            "description": (
                "Deduct points from a family member as a penalty for bad behaviour. "
                "Use when a parent says someone was rude, lied, fought, used bad language, "
                "was disobedient, disrespectful, or explicitly asks to deduct/remove points. "
                "Use penalty_id from the configured list; for unlisted reasons use custom_reason + custom_points."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person": {
                        "type": "string",
                        "description": "First name of the person to penalise.",
                    },
                    "penalty_id": {
                        "type": "string",
                        "description": "Preset penalty reason ID, e.g. rude_behaviour, lying, fighting, disobedience, bad_language, disrespect, damaging_property.",
                    },
                    "custom_reason": {
                        "type": "string",
                        "description": "Free-text reason if no preset penalty_id matches.",
                    },
                    "custom_points": {
                        "type": "integer",
                        "description": "Points to deduct when using a custom reason (positive number).",
                    },
                },
                "required": ["person", "penalty_id"],
            },
        },
    },
]


# Drop scoreboard-related tools when feature is disabled
try:
    from avatar_backend.config import get_settings as _cfg_gs
    if not _cfg_gs().scoreboard_enabled:
        _sb_tools = {"log_chore", "get_scoreboard", "deduct_points"}
        HA_TOOLS = [t for t in HA_TOOLS if t["function"]["name"] not in _sb_tools]
except Exception:
    pass

# Anthropic uses a slightly different tool schema format
_ANTHROPIC_TOOLS: list[dict] = [
    {
        "name":         t["function"]["name"],
        "description":  t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in HA_TOOLS
]
