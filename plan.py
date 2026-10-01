"""Render a survey floor plan with element markers + coordinate grid, so placement can be discussed visually."""
import io
from pathlib import Path
import httpx
from PIL import Image, ImageDraw, ImageFont

SYS_COLORS = {1: "#faaf40", 2: "#00a650", 3: "#e76e34", 4: "#0e67aa", 5: "#d40b15", 6: "#559df6", 7: "#db70db", 8: "#fe8402", 9: "#34d1ed", 10: "#732112"}


def fetch_image(url):
    r = httpx.get(url, follow_redirects=True, timeout=60)
    if r.status_code in (403, 404):
        raise RuntimeError("raw image URL not directly readable; use fetch_image_signed")
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGBA")


def fetch_image_signed(client, doc):
    key = doc["floorplan_url"].split("media/")[1]
    u = client.get("/storage/media/presign", params={"key": key})["url"]
    r = httpx.get(u, follow_redirects=True, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGBA")


def render(doc, out_path, client=None, grid=100, labels=True, scale=1.5, highlight_missing_model=False, extra_points=None, icon_colors=True):
    """extra_points: [{x,y,label}] drawn as red crosses (proposed placements)."""
    img = fetch_image_signed(client, doc)
    W, H = img.size
    img = img.resize((int(W * scale), int(H * scale)))
    d = ImageDraw.Draw(img, "RGBA")
    try:
        f = ImageFont.truetype("arial.ttf", 11)
        fb = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        f = fb = ImageFont.load_default()
    if grid:
        for gx in range(0, W, grid):
            d.line([(gx * scale, 0), (gx * scale, H * scale)], fill=(0, 120, 255, 90), width=1)
            d.text((gx * scale + 2, 2), str(gx), fill=(0, 90, 220, 255), font=f)
        for gy in range(0, H, grid):
            d.line([(0, gy * scale), (W * scale, gy * scale)], fill=(0, 120, 255, 90), width=1)
            d.text((2, gy * scale + 2), str(gy), fill=(0, 90, 220, 255), font=f)
    for e in doc.get("elements", []):
        p = e.get("position")
        if not isinstance(p, dict):
            continue
        x, y = p["x"] * scale, p["y"] * scale
        col = SYS_COLORS.get(e.get("systemtype_id"), "#888888")
        if icon_colors:
            ic = next((str(a.get("value") or "") for a in e.get("attributes", []) if a["attribute_id"] == 530), "")
            if len(ic) == 6 and all(c in "0123456789abcdefABCDEF" for c in ic):
                col = "#" + ic
        has_model = any(a["attribute_id"] == 305 and a.get("value") for a in e.get("attributes", []))
        r = 6
        outline = (0, 0, 0, 255) if has_model or not highlight_missing_model else (255, 0, 0, 255)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col, outline=outline, width=2)
        if labels:
            name = e.get("name", "")
            d.text((x + r + 2, y - 6), name, fill=(0, 0, 0, 255), font=f)
    for pt in extra_points or []:
        x, y = pt["x"] * scale, pt["y"] * scale
        d.line([(x - 7, y - 7), (x + 7, y + 7)], fill=(255, 0, 0, 255), width=3)
        d.line([(x - 7, y + 7), (x + 7, y - 7)], fill=(255, 0, 0, 255), width=3)
        d.text((x + 9, y - 6), str(pt.get("label", "")), fill=(200, 0, 0, 255), font=fb)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path)
    return {"path": str(out_path), "plan_px": [W, H], "grid": grid}
