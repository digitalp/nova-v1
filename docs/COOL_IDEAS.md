# Nova — Cool Feature Ideas

## Smart Energy Automations
- **Octopus off-peak charging** — automate heavy loads (washer, dishwasher, EV) to run during off-peak tariff windows using Octopus Energy integration
- **Solar/battery optimization** — shift loads to peak generation hours if solar panels are added

## Comfort Automations
- **Adaptive lighting** — auto-adjust light color temperature throughout the day (warm at night, cool during day)
- **Sleep quality tracker** — combine bedroom temperature, humidity, and presence sensors to log sleep patterns and auto-adjust heating/ventilation

## Security Enhancements
- **Geofence-based alarm** — auto-arm when all phones leave, auto-disarm on arrival
- **Suspicious activity pattern** — motion detected at unusual hours (2-5am) on outdoor cameras → escalate to phone notification + record clip

## Nova-Specific Features
- **Daily energy report** — "You used X kWh today, Y% more than yesterday. The washer was the biggest consumer." Spoken at dinner time
- **Weekly home health digest** — Sunday morning summary: battery levels, sensor anomalies, heating efficiency, camera events of the week
- **Music by voice** — "Play some jazz" → Nova searches Music Assistant and plays on the nearest speaker
- **Goodnight routine** — "Goodnight Nova" → locks doors, turns off lights, sets heating to night mode, arms cameras, confirms with summary
- **Family scoreboard** — gamify chores: "Nova, I emptied the dishwasher" → tracks points per person, weekly leaderboard on avatar page
- **Multi-room awareness** — different avatar instances on tablets in each room, Nova knows which room you're in and routes audio/responses to that room only
- **Predictive automations** — Nova learns patterns (lights on at 6pm, heating up at 7am) and suggests automations: "I notice you turn on the hallway light every evening at 6. Want me to automate that?"

## Notification Channels
- **WhatsApp notifications** — add WhatsApp as a first-class outbound channel for urgent alerts, away notifications, and optional self-heal updates
  - **Best architecture** — implement a small `WhatsAppService` plus a generic notification-channel wrapper so Nova can route alerts to speakers, mobile push, Telegram, and WhatsApp cleanly instead of hardcoding one-off behavior
  - **Provider options** — Meta WhatsApp Cloud API for production, Twilio WhatsApp for fastest prototype, or Home Assistant if a working WhatsApp notify path already exists there
  - **First integration points** — proactive away alerts, critical fault notifications, and announce fallback when nobody is home
  - **Config needed** — provider, API/token credentials, sender/recipient fields, enabled flag, and optional delivery policy like `urgent only` or `away only`
  - **Nice follow-up** — admin UI test button plus routing rules so Nova can choose between push, WhatsApp, Telegram, and spoken alerts depending on urgency and presence
