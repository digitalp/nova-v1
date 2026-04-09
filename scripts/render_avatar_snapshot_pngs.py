from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1365
HEIGHT = 768
OUT_DIR = Path("/opt/avatar-server/docs/screenshots")
FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"

SCENES = {
    "avatar_active_event.png": {
        "label": "Active Event",
        "status": "Speaking with active camera popup",
        "subtitle": "Nova: The driveway vehicle is still outside and the package has not been collected.",
        "popup_title": "Driveway Vehicle",
        "chips": [("ACTIVE", "#1e40af", "#dbeafe"), ("NEEDS REVIEW", "#92400e", "#fef3c7")],
        "popup_msg": "A dark vehicle has remained in the driveway longer than expected. Front door package is still visible.",
        "popup_note": "Open loop: package still outside and vehicle remains parked.",
        "actions": [("Ask about the vehicle", "#a16207"), ("Show related camera", "#0f172a"), ("Resolve", "#166534")],
        "cards": [
            ("ACTIVE", "Front Door Delivery", "Package left near the front door 12 minutes ago.", "Still visible from the front camera.", ["Acknowledge", "Resolve"]),
            ("ACKNOWLEDGED", "Driveway Motion", "Vehicle arrived and remains stationary.", "Acknowledged but still unresolved.", ["Reopen", "Resolve"]),
            ("RESOLVED", "Outdoor Motion", "Short motion event near the side gate.", "Closed out after no follow-up activity.", ["View"]),
        ],
    },
    "avatar_confirmation_flow.png": {
        "label": "Confirmation Flow",
        "status": "Confirming a state-changing event action",
        "subtitle": "Nova: Confirm before dismissing the unresolved delivery event.",
        "popup_title": "Front Door Delivery",
        "chips": [("ACKNOWLEDGED", "#92400e", "#fef3c7")],
        "popup_msg": "The package event is waiting for confirmation before it is dismissed from the active surface.",
        "popup_note": "Open loop: unresolved until someone confirms it was handled.",
        "confirm": True,
        "cards": [
            ("ACKNOWLEDGED", "Front Door Delivery", "Package event is pending confirmation for dismissal.", "Needs an explicit operator decision.", ["Resolve", "Dismiss"]),
            ("ACTIVE", "Driveway Vehicle", "Vehicle still present in driveway view.", "Camera follow-up remains available.", ["Ask", "Show Camera"]),
            ("RESOLVED", "Door Motion", "Walkway motion event cleared.", "Resolved after visual review.", ["View"]),
        ],
    },
    "avatar_recent_event_triage.png": {
        "label": "Recent Event Triage",
        "status": "Reviewing unresolved-first recent event stack",
        "subtitle": "Nova: Recent events are grouped with unresolved items first and clear status labels.",
        "popup_title": "Recent Event Review",
        "chips": [("SURFACE STATE", "#1e40af", "#dbeafe")],
        "popup_msg": "The active popup is minimized here so the recent-event strip can be reviewed as the primary triage surface.",
        "popup_note": "Status chips, open-loop notes, and quick actions stay visible without reopening each event.",
        "actions": [("Acknowledge", "#a16207"), ("Dismiss", "#0f172a"), ("Resolve", "#166534")],
        "cards": [
            ("ACTIVE", "Package Delivery", "Package remains on the doorstep after 18 minutes.", "Unresolved items stay pinned to the front of the strip.", ["Ask", "Resolve"]),
            ("ACTIVE", "Driveway Vehicle", "Driveway camera still sees a parked vehicle.", "Follow-up action can hop to related camera views.", ["Show Camera", "Acknowledge"]),
            ("ACKNOWLEDGED", "Outdoor Motion", "Motion near the side gate was seen and acknowledged.", "Still visible because it has not been resolved.", ["Resolve", "Dismiss"]),
        ],
    },
}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD_PATH if bold else FONT_PATH
    return ImageFont.truetype(path, size)


FONT_12 = load_font(12)
FONT_13 = load_font(13)
FONT_14 = load_font(14)
FONT_16 = load_font(16, bold=True)
FONT_18 = load_font(18, bold=True)
FONT_24 = load_font(24, bold=True)


def rounded(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def chip(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill: str, fg: str) -> int:
    w = int(draw.textlength(text, font=FONT_12)) + 18
    rounded(draw, (x, y, x + w, y + 24), 12, fill, outline="#475569")
    draw.text((x + 9, y + 5), text, font=FONT_12, fill=fg)
    return w


def button(draw: ImageDraw.ImageDraw, box, text: str, fill: str):
    rounded(draw, box, 12, fill, outline="#334155")
    tw = draw.textlength(text, font=FONT_13)
    draw.text((box[0] + (box[2] - box[0] - tw) / 2, box[1] + 10), text, font=FONT_13, fill="#e2e8f0")


def render_scene(name: str, scene: dict):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#08101a")
    draw = ImageDraw.Draw(image)

    for y in range(HEIGHT):
        t = y / HEIGHT
        r = int(8 * (1 - t) + 5 * t)
        g = int(16 * (1 - t) + 9 * t)
        b = int(26 * (1 - t) + 18 * t)
        draw.line((0, y, WIDTH, y), fill=(r, g, b))

    rounded(draw, (20, 20, WIDTH - 20, HEIGHT - 20), 24, None, outline="#1e293b")
    rounded(draw, (WIDTH // 2 - 52, 16, WIDTH // 2 + 52, 44), 18, "#050a12", outline="#334155")
    draw.text((WIDTH // 2 - 20, 23), "Nova", font=FONT_16, fill="#67e8f9")
    draw.text((WIDTH - 140, 30), scene["label"].upper(), font=FONT_12, fill="#94a3b8")

    draw.ellipse((340, 120, 1020, 690), fill="#0b1321", outline="#0f172a")
    draw.ellipse((450, 170, 910, 620), fill="#13253d", outline="#1d4ed8")
    draw.ellipse((520, 220, 840, 540), fill="#0f172a", outline="#22d3ee")

    rounded(draw, (26, HEIGHT - 64, 320, HEIGHT - 22), 20, "#050a12", outline="#1e40af")
    draw.ellipse((40, HEIGHT - 51, 52, HEIGHT - 39), fill="#38bdf8")
    draw.text((66, HEIGHT - 56), scene["status"], font=FONT_14, fill="#dbeafe")

    rounded(draw, (260, HEIGHT - 70, WIDTH - 260, HEIGHT - 22), 14, "#000000", outline="#334155")
    draw.text((284, HEIGHT - 58), scene["subtitle"], font=FONT_14, fill="#e2e8f0")

    popup = (990, 104, 1340, 476)
    rounded(draw, popup, 18, "#070c16", outline="#164e63")
    rounded(draw, (popup[0], popup[1], popup[2], popup[1] + 46), 18, "#1f2937")
    draw.text((popup[0] + 16, popup[1] + 12), scene["popup_title"], font=FONT_16, fill="#e2e8f0")

    chip_x = popup[0] + 14
    for text, fill, fg in scene["chips"]:
        chip_x += chip(draw, chip_x, popup[1] + 58, text, fill, fg) + 8

    draw.multiline_text((popup[0] + 14, popup[1] + 98), scene["popup_msg"], font=FONT_14, fill="#cbd5e1", spacing=4)
    draw.multiline_text((popup[0] + 14, popup[1] + 152), scene["popup_note"], font=FONT_13, fill="#93c5fd", spacing=4)

    if scene.get("confirm"):
        rounded(draw, (popup[0] + 14, popup[1] + 198, popup[2] - 14, popup[1] + 286), 12, "#0f172a", outline="#a16207")
        draw.multiline_text((popup[0] + 28, popup[1] + 212), "Dismissing this event will hide it from the active surface while leaving it unresolved in history.", font=FONT_13, fill="#e2e8f0", spacing=4)
        button(draw, (popup[0] + 24, popup[1] + 250, popup[0] + 162, popup[1] + 284), "Confirm Dismiss", "#92400e")
        button(draw, (popup[0] + 174, popup[1] + 250, popup[0] + 312, popup[1] + 284), "Cancel", "#0f172a")
    else:
        y = popup[1] + 198
        for text, fill in scene["actions"]:
            button(draw, (popup[0] + 14, y, popup[2] - 14, y + 38), text, fill)
            y += 46

    rounded(draw, (popup[0] + 14, popup[1] + 298, popup[2] - 14, popup[1] + 356), 12, "#334155", outline="#475569")
    draw.text((popup[0] + 124, popup[1] + 319), "Camera Feed", font=FONT_16, fill="#e2e8f0")

    draw.text((24, 426), "RECENT EVENTS", font=FONT_12, fill="#93c5fd")
    card_x = 24
    for status, title, text, note, actions in scene["cards"]:
        box = (card_x, 448, card_x + 420, 710)
        outline = {"ACTIVE": "#1d4ed8", "ACKNOWLEDGED": "#a16207", "RESOLVED": "#166534"}[status]
        rounded(draw, box, 18, "#08101a", outline=outline)
        draw.text((card_x + 14, 464), title, font=FONT_16, fill="#e2e8f0")
        chip(draw, card_x + 14, 492, status, outline, "#f8fafc")
        draw.multiline_text((card_x + 14, 528), text, font=FONT_14, fill="#cbd5e1", spacing=4)
        draw.multiline_text((card_x + 14, 586), note, font=FONT_13, fill="#93c5fd", spacing=4)
        ax = card_x + 14
        for label in actions:
            w = int(draw.textlength(label, font=FONT_12)) + 24
            rounded(draw, (ax, 650, ax + w, 678), 14, "#0e7490", outline="#164e63")
            draw.text((ax + 12, 657), label, font=FONT_12, fill="#e0f2fe")
            ax += w + 8
        card_x += 442

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    image.save(OUT_DIR / name)


def main():
    for name, scene in SCENES.items():
        render_scene(name, scene)


if __name__ == "__main__":
    main()
