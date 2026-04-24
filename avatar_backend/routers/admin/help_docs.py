"""Help & Tips — serves rendered markdown docs from docs/ directory."""
from __future__ import annotations
import json
import markdown
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from .common import _require_session

router = APIRouter()

_DOCS_DIR = Path(__file__).parent.parent.parent.parent / "docs"

# Docs shown in the Help section (in display order)
_FEATURED = [
    ("USER_MANUAL.md",           "User Manual"),
    ("ENROLLMENT_GUIDE.md",      "Device Enrollment"),
    ("HA_INTEGRATION_OVERVIEW.md", "Home Assistant Integration"),
]

_MD_EXTENSIONS = ["tables", "fenced_code", "toc", "nl2br", "attr_list"]


def _render(path: Path) -> str:
    return markdown.markdown(path.read_text(encoding="utf-8"), extensions=_MD_EXTENSIONS)


@router.get("/help/docs")
async def list_help_docs(request: Request):
    """Return ordered list of available docs."""
    _require_session(request, min_role="viewer")
    docs = []
    seen = set()
    for fname, title in _FEATURED:
        p = _DOCS_DIR / fname
        if p.exists():
            docs.append({"name": fname, "title": title, "generated": False})
            seen.add(fname)
    # Also surface any auto-generated docs
    gen_dir = _DOCS_DIR / "generated"
    if gen_dir.exists():
        for p in sorted(gen_dir.glob("*.md")):
            if p.name not in seen:
                docs.append({
                    "name": "generated/" + p.name,
                    "title": p.stem.replace("_", " ").title(),
                    "generated": True,
                })
    return JSONResponse({"docs": docs})


@router.get("/help/docs/{name:path}")
async def get_help_doc(name: str, request: Request):
    """Return a doc rendered as HTML."""
    _require_session(request, min_role="viewer")
    # Only allow .md files within docs/
    clean = name.replace("..", "").lstrip("/")
    if not clean.endswith(".md"):
        return JSONResponse({"error": "not found"}, status_code=404)
    p = _DOCS_DIR / clean
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    html = _render(p)
    return HTMLResponse(content=html)
