"""System Surveyor MCP server. Writes are dry-run unless apply=true."""
import base64, collections, functools, hashlib, hmac, json, os, re, threading, time
from pathlib import Path
import httpx
from mcp.server.fastmcp import FastMCP
from ssapi import SSAuthError, Client, SSError
import quote as Q
import plan as P

from mcp.server.transport_security import TransportSecuritySettings

INSTRUCTIONS = """System Surveyor (floor-plan site surveys) for a security-integration company's design team.
ids: site_id and survey_id are UUID strings (list_sites -> list_surveys -> survey id).
Typical jobs:
- "BOM / quote from this survey": find the survey, call bom (gaps list what is missing), then quote. bom/quote return cpq_items in the exact shape the CPQ matching tool (e.g. a Salesforce MCP's cpq_match_parts) takes (model, manufacturer, description, quantity) - pass them straight through to price against Salesforce CPQ and then cpq_add_lines.
- "Put model numbers in": survey_gaps -> find_products -> assign_models (profile_id copies model, manufacturer, price). Ask the user which product when more than one fits; never guess.
- "Place this Salesforce BOM": read the quote with salesforce-mcp cpq_read_quote, pass its lines to match_parts, then render_plan and ask the user where each part goes, then place_elements with x/y in plan pixels.
Every change tool is a dry run unless apply=true. Writes are refused unless the server has them enabled AND the survey is on its allowlist, and are capped per save. Rules for you: never call apply=true until you have shown the dry-run list to the user and they said yes in this conversation; if anything is ambiguous (which model, indoor/outdoor, where a part goes, duplicate names, a price that might be cost vs list) ask first, never guess; change only what was asked and only the survey named; never delete unless explicitly told. Each save takes a snapshot first, then re-reads the survey and reports verified true/false; if anything is off, restore_backup undoes it (call list_backups). Writes only work on the owner's own surveys (or ones he allowlisted), max 40 elements per save. Call status if something fails. Tools for changing a survey: assign_models (model/manufacturer/price; copy_specs=true also copies product specs but never the surveyed mount height/coverage/frame rates), rename_elements (fix_duplicates=true numbers duplicates correctly), place_elements, move_elements, delete_elements (only when told to), restore_backup.
Colors: set_colors changes icon colors (and coverage/field-of-view colors, including each lens of a multi-lens camera) without touching surveyed geometry - ask the user which scheme (one color for everything, by model, by type, by status, by system, or per-lens colors) and show the color_map from the dry run before applying. set_attributes sets any attribute on a group (status, description, mount height) with old values shown. survey_summary gives a quick overview. BOM placement: when the user gives a BOM (or a Salesforce quote), call propose_placement, show the plan and ASK the user every question it returns (never choose locations yourself), use render_plan with the proposed points so they can confirm spots, then apply_placement dry run, yes, apply. Prefer assigning BOM parts to existing blank elements over adding new ones.
Unknown values stay unknown: report gaps, do not invent prices or model numbers."""

mcp = FastMCP("systemsurveyor", instructions=INSTRUCTIONS, host="0.0.0.0", port=int(os.environ.get("PORT", 8797)),
              stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
C = Client()
OUT = Path(os.environ.get("DATA_DIR") or (Path.home() / ".systemsurveyor"))
OUT.mkdir(parents=True, exist_ok=True)
WRITES = os.environ.get("ALLOW_WRITES", "false").lower() == "true"
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", 0) or 0)
WRITE_SURVEYS = {x.strip() for x in os.environ.get("WRITE_SURVEYS", "").split(",") if x.strip()}
DEF_LABOR = float(os.environ.get("LABOR_RATE", -1))
DEF_CABLE = float(os.environ.get("CABLE_PER_FT", -1))
DEF_MARKUP = float(os.environ.get("MARKUP_PCT", 0))
DEF_TAX = float(os.environ.get("TAX_PCT", 0))
MAX_ELEMENTS = int(os.environ.get("MAX_ELEMENTS_PER_WRITE", 40))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
_wlock = threading.Lock()


def _may_write(live):
    """Only surveys created by OWNER_USER_ID on this server's team, or ones explicitly allowlisted in WRITE_SURVEYS. Nothing else."""
    if live["id"] in WRITE_SURVEYS:
        return None
    if live.get("team_id") != C.team_id():
        return f"survey belongs to team {live.get('team_id')}, not this server's team"
    if OWNER_USER_ID and live.get("creator") == OWNER_USER_ID:
        return None
    return "survey was created by someone else and is not on the allowlist (WRITE_SURVEYS). Nothing changed."


def _norm_attrs(e):
    return {a["attribute_id"]: C._sval(a.get("value")) for a in e.get("attributes", [])}


def _verify(d, live):
    """Re-read after saving: every element we meant to save must be there with the same attributes."""
    want = {e["id"]: e for e in d.get("elements", [])}
    got = {e["id"]: e for e in live.get("elements", [])}
    bad = [i for i in want if i not in got] + [i for i in got if i not in want]
    for i, e in want.items():
        if i in got and _norm_attrs(e) != _norm_attrs(got[i]):
            bad.append(i)
    return {"ok": not bad, "mismatched_ids": bad[:10], "elements": len(got)}


def _commit(d, tool, extra, force_count=None):
    """Guardrails, then snapshot, then save, then verify. Dry-run callers never reach here with apply=false."""
    if not WRITES:
        return {"saved": False, "blocked": "Writes are switched off on this server (ALLOW_WRITES=false). Nothing was changed; the dry run above is what would happen."}
    n = force_count if force_count is not None else (extra.get("count") or len(extra.get("added") or []) or extra.get("moves") or extra.get("deleted") or 0)
    if n > MAX_ELEMENTS:
        return {"saved": False, "blocked": f"{n} elements in one save exceeds the limit of {MAX_ELEMENTS}. Split the change into smaller batches."}
    with _wlock:
        live = C.survey(d["id"])
        why = _may_write(live)
        if why:
            return {"saved": False, "blocked": why}
        if live.get("version") != d.get("version"):
            return {"saved": False, "error": "survey changed while working; re-read and redo"}
        ed = live.get("editor")
        ed = ed.get("user_id") if isinstance(ed, dict) else ed
        if ed not in (None, "", 0) and OWNER_USER_ID and ed != OWNER_USER_ID:
            return {"saved": False, "blocked": f"someone else (user {ed}) has this survey claimed for editing. Nothing changed."}
        keep = bool(ed) and ed == OWNER_USER_ID
        bk = OUT / "backups"
        bk.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snap = {k: v for k, v in live.items() if k != "floorplan_url"}
        (bk / f"{d['id']}-{stamp}.json").write_text(json.dumps(snap))
        res = C.save_survey(d, keep_lock=keep)
        after = C.survey(d["id"])
        ver = _verify(d, after)
        with open(OUT / "journal.jsonl", "a") as f:
            f.write(json.dumps({"t": stamp, "tool": tool, "survey": d["id"], "title": d.get("title"), "n": n, "verified": ver["ok"]}) + "\n")
        out = {"saved": True, **extra, "backup": f"{d['id']}-{stamp}.json", "verified": ver}
        if not ver["ok"]:
            out["warning"] = "saved, but the re-read does not match what was sent. Check the survey; the backup can restore it (restore_backup)."
        return out


def _link(path, hours=24):
    if not PUBLIC_URL:
        return str(path)
    name = Path(path).name
    exp = int(time.time()) + hours * 3600
    sig = hmac.new(MCP_TOKEN.encode(), f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{PUBLIC_URL}/dl/{name}?exp={exp}&sig={sig}"
ATTR = {"id": 141, "label": 173, "status": 138, "color": 530, "manufacturer": 271, "model": 305, "qty": 531, "price": 532, "hours": 533}
CABLE = 65


def _a(e, attr_id):
    return C.get_attr(e, attr_id)


def _pal():
    return C.palette()["by_id"]


def _brief(e, pal):
    p = e.get("position") or {}
    return {
        "id": e["id"],
        "name": e.get("name"),
        "type": pal.get(e.get("element_id"), {}).get("name", e.get("element_id")),
        "element_id": e.get("element_id"),
        "x": round(p.get("x", 0), 1) if isinstance(p, dict) else None,
        "y": round(p.get("y", 0), 1) if isinstance(p, dict) else None,
        "status": _a(e, ATTR["status"]),
        "manufacturer": _a(e, ATTR["manufacturer"]),
        "model": _a(e, ATTR["model"]),
        "price": _a(e, ATTR["price"]),
        "label": _a(e, ATTR["label"]),
    }


def _guard(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except SSError as e:
            return {"error": str(e)}
    return w


# attributes that describe the surveyed design, never overwritten from a preset
SURVEYED = {141, 140, 138, 530, 531, 533, 167, 298, 299, 456, 457, 458, 459, 633, 642, 643, 644, 179, 178, 257}


def _pattrs(p):
    return {a["attribute_id"]: a["value"] for a in p["content"]["attribute"]}


def _prow(p):
    a = _pattrs(p)
    return {"profile_id": p["id"], "name": p["name"], "element_id": p["element_id"],
            "manufacturer": a.get(271), "model": a.get(305), "price": a.get(532)}


# ---------------- read ----------------
@mcp.tool()
@_guard
def list_sites(search: str = "", favorites_only: bool = False) -> list:
    """List sites (id, name, survey_count). Optional name filter."""
    return [{"id": s["id"], "name": s["name"], "surveys": s.get("survey_count"), "modified": s.get("modified_at")}
            for s in C.sites(search or None, favorites_only)]


@mcp.tool()
@_guard
def list_surveys(site_id: str) -> list:
    """List the surveys (floor plans) of a site. site_id is the UUID string from list_sites."""
    return [{"id": s["id"], "title": s["title"], "status": s.get("status"), "elements": s.get("element_count"), "modified": s.get("modified_at")}
            for s in C.surveys(site_id)]


@mcp.tool()
@_guard
def get_survey(survey_id: str, elements: bool = True, limit: int = 300) -> dict:
    """Survey summary plus a compact list of elements (id, name, type, x/y plan pixels, status, manufacturer, model, price)."""
    d = C.survey(survey_id)
    pal = _pal()
    out = {k: d.get(k) for k in ("id", "title", "unit", "floorplan_scale", "icon_size", "version", "modified_at", "modified_source", "summary")}
    out["site"] = d.get("site")
    els = d.get("elements", [])
    out["element_count"] = len(els)
    if elements:
        out["elements"] = [_brief(e, pal) for e in els[:limit]]
    return out


@mcp.tool()
@_guard
def list_palette(query: str = "", system: str = "") -> list:
    """Element types you can place (element_id, name, system). system e.g. video_surveillance, access_control."""
    q, s = query.lower(), system.lower()
    return [p for p in _pal().values() if q in p["name"].lower() and s in p["system"]]


@mcp.tool()
@_guard
def list_profiles(query: str = "", element_id: int = 0, limit: int = 40) -> list:
    """Saved product presets (profile id, name, element_id, manufacturer, model, price). profile_id works in place_elements / assign_models."""
    out = []
    for p in C.profiles():
        row = _prow(p)
        hay = f'{row["name"]} {row["manufacturer"]} {row["model"]}'.lower()
        if query.lower() in hay and (not element_id or element_id == p["element_id"]):
            out.append(row)
    return out[:limit]


@mcp.tool()
@_guard
def find_products(query: str, element_id: int = 0, limit: int = 15) -> list:
    """Fuzzy search the team's product presets by words / part number (ranked). Use to pick a model for an element."""
    toks = [t for t in re.split(r"\W+", query.lower()) if t]
    nq = Q.norm(query)
    scored = []
    for p in C.profiles():
        if element_id and p["element_id"] != element_id:
            continue
        row = _prow(p)
        hay = f'{row["name"]} {row["manufacturer"]} {row["model"]}'.lower()
        s = sum(1 for t in toks if t in hay)
        if nq and Q.norm(row["model"]) == nq:
            s += 10
        if s:
            scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


@mcp.tool()
@_guard
def survey_gaps(survey_id: str) -> dict:
    """What's missing on a survey: per element type, how many lack manufacturer/model/price; duplicate IDs; cable paths without type/length."""
    d = C.survey(survey_id)
    pal = _pal()
    by = collections.defaultdict(lambda: {"count": 0, "no_manufacturer": 0, "no_model": 0, "no_price": 0, "names_without_model": []})
    ids = collections.Counter()
    cable = []
    for e in d.get("elements", []):
        t = pal.get(e.get("element_id"), {}).get("name", str(e.get("element_id")))
        ids[_a(e, 141)] += 1
        if e.get("element_id") == CABLE:
            if not _a(e, 526) or not _a(e, 524):
                cable.append({"name": e.get("name"), "cable_type": _a(e, 526), "length_ft": _a(e, 524)})
            continue
        r = by[t]
        r["count"] += 1
        if not _a(e, 271):
            r["no_manufacturer"] += 1
        if not _a(e, 305):
            r["no_model"] += 1
            r["names_without_model"].append(e.get("name"))
        if not _a(e, 532):
            r["no_price"] += 1
    return {"title": d.get("title"), "by_type": dict(by),
            "duplicate_ids": [k for k, v in ids.items() if k and v > 1], "cable_paths_incomplete": cable}


# ---------------- model numbers ----------------
@mcp.tool()
@_guard
def assign_models(survey_id: str, ids: list[str] = [], element_id: int = 0, name_prefix: str = "", only_missing: bool = True,
                  profile_id: int = 0, manufacturer: str = "", model: str = "", price: float = -1, description: str = "",
                  attributes: dict = {}, copy_specs: bool = False, apply: bool = False) -> dict:
    """Put model numbers / manufacturer / price on existing elements.
    Select targets by ids (uuid or name like FCAM-001), and/or element_id (type), and/or name_prefix. only_missing skips elements that already have a model.
    Values come from a saved preset (profile_id - copies only manufacturer, model, price and description so the surveyed coverage angle, direction and mount height are kept; copy_specs=true also copies the preset's product specs - resolution, lens, shell, IR, encoding, etc. - but NEVER the surveyed values: id, status, color, qty, hours, mount height, coverage angle/direction/radius, frame rates) and/or explicit manufacturer/model/price/description/attributes{attr_id:value}.
    Dry run unless apply=true."""
    d = C.survey(survey_id)
    prof = None
    if profile_id:
        prof = next((p for p in C.profiles() if p["id"] == profile_id), None)
        if not prof:
            return {"error": f"unknown profile_id {profile_id}"}
    want = set(ids)
    targets = []
    for e in d.get("elements", []):
        if want and e["id"] not in want and e.get("name") not in want and _a(e, 141) not in want:
            continue
        if not want and not (element_id or name_prefix):
            return {"error": "give ids, element_id or name_prefix so I know which elements to change"}
        if element_id and e.get("element_id") != element_id:
            continue
        if name_prefix and not str(e.get("name", "")).startswith(name_prefix):
            continue
        if prof and e.get("element_id") != prof["element_id"]:
            continue
        if only_missing and _a(e, 305):
            continue
        targets.append(e)
    vals = {}
    if prof:
        keep = {271, 305, 532, 173}
        vals.update({k: v for k, v in _pattrs(prof).items() if v not in ("", None) and k not in SURVEYED and (copy_specs or k in keep)})
    if manufacturer:
        vals[271] = manufacturer
    if model:
        vals[305] = model
    if price >= 0:
        vals[532] = price
    if description:
        vals[173] = description
    vals.update({int(k): v for k, v in attributes.items()})
    if not vals:
        return {"error": "nothing to set: pass profile_id or model/manufacturer/price/description/attributes"}
    if not targets:
        return {"error": "no matching elements (check only_missing / filters)"}
    pal = _pal()
    for e in targets:
        for k, v in vals.items():
            C.set_attr(e, k, v)
        if prof:
            e["element_profile_id"] = prof["id"]
    preview = [_brief(e, pal) for e in targets]
    if not apply:
        return {"dry_run": True, "count": len(targets), "set": {C.attr_name(k): v for k, v in vals.items()}, "would_update": preview}
    return _commit(d, "assign_models", {"count": len(targets), "updated": preview})


@mcp.tool()
@_guard
def place_elements(survey_id: str, items: list[dict], apply: bool = False) -> dict:
    """Place parts on a floor plan. items: [{element_id?, x, y, name?, profile_id?, manufacturer?, model?, price?, description?, attributes?: {attr_id: value}}].
    x/y are plan pixels (see render_plan). IDs (FCAM-016...) auto-number. profile_id fills model/manufacturer/price. Dry run unless apply=true."""
    d = C.survey(survey_id)
    profs = {p["id"]: p for p in C.profiles()}
    new = []
    for i in items:
        prof = None
        if i.get("profile_id"):
            prof = profs.get(i["profile_id"])
            if not prof:
                return {"error": f"unknown profile_id {i['profile_id']}"}
        eid = i.get("element_id") or (prof or {}).get("element_id")
        if not eid:
            return {"error": "each item needs element_id or profile_id"}
        at = {int(k): v for k, v in (i.get("attributes") or {}).items()}
        for key, aid in (("manufacturer", 271), ("model", 305), ("price", 532), ("description", 173)):
            if i.get(key) not in (None, ""):
                at[aid] = i[key]
        el = C.new_element(d, eid, i["x"], i["y"], i.get("name"), at, prof)
        d["elements"].append(el)
        new.append(el)
    preview = [_brief(e, _pal()) for e in new]
    if not apply:
        return {"dry_run": True, "would_add": preview}
    return _commit(d, "place_elements", {"added": preview})


@mcp.tool()
@_guard
def move_elements(survey_id: str, moves: list[dict], apply: bool = False) -> dict:
    """Move elements. moves: [{id, x, y}] (element uuid from get_survey). Dry run unless apply=true."""
    d = C.survey(survey_id)
    by = {e["id"]: e for e in d["elements"]}
    for m in moves:
        if m["id"] not in by:
            return {"error": f"element {m['id']} not found"}
        by[m["id"]]["position"] = {"x": float(m["x"]), "y": float(m["y"])}
    if not apply:
        return {"dry_run": True, "moves": moves}
    return _commit(d, "move_elements", {"moves": len(moves)})


@mcp.tool()
@_guard
def rename_elements(survey_id: str, renames: list[dict] = [], fix_duplicates: bool = False, apply: bool = False) -> dict:
    """Rename elements (their ID, attribute 141). renames: [{id: uuid, name: "FCAM-015"}]. fix_duplicates=true finds elements that share a name and gives every later one the next free number for its type (the first one in list order keeps its name). Names never collide. Dry run unless apply=true."""
    d = C.survey(survey_id)
    els = d.get("elements", [])
    plan = []
    if fix_duplicates:
        used = {str(_a(e, 141) or e.get("name")) for e in els}
        seen = set()
        for e in els:
            nm = str(_a(e, 141) or e.get("name"))
            if nm in seen:
                new = C.next_id(d, e["element_id"], extra=used)
                used.add(new)
                plan.append((e, nm, new))
            seen.add(nm)
    byid = {e["id"]: e for e in els}
    taken = {str(_a(e, 141)) for e in els}
    for r in renames:
        e = byid.get(r.get("id"))
        if not e:
            return {"error": f"unknown element id {r.get('id')}"}
        if r["name"] in taken and r["name"] != _a(e, 141):
            return {"error": f"name {r['name']} already used"}
        taken.add(r["name"])
        plan.append((e, _a(e, 141), r["name"]))
    if not plan:
        return {"error": "nothing to rename"}
    for e, old, new in plan:
        C.set_attr(e, 141, new)
        e["name"] = new
    out = [{"id": e["id"], "from": old, "to": new, "x": round((e.get("position") or {}).get("x", 0)), "y": round((e.get("position") or {}).get("y", 0))} for e, old, new in plan]
    if not apply:
        return {"dry_run": True, "count": len(out), "renames": out}
    return _commit(d, "rename_elements", {"count": len(out), "renames": out})


@mcp.tool()
@_guard
def delete_elements(survey_id: str, ids: list[str], apply: bool = False) -> dict:
    """Delete elements by uuid. Dry run unless apply=true."""
    d = C.survey(survey_id)
    have = {e["id"] for e in d["elements"]}
    missing = [i for i in ids if i not in have]
    if missing:
        return {"error": f"not found: {missing}"}
    d["elements"] = [e for e in d["elements"] if e["id"] not in set(ids)]
    if not apply:
        return {"dry_run": True, "would_delete": ids}
    return _commit(d, "delete_elements", {"deleted": len(ids)})


# ---------------- colors + bulk attributes + BOM placement ----------------
NAMED = {"red": "E6003E", "green": "2EB82E", "blue": "1A6BD7", "yellow": "F8C309", "orange": "FE8402", "purple": "9B59B6",
         "pink": "DB70DB", "cyan": "34D1ED", "teal": "0E9AA7", "brown": "732112", "gray": "888888", "grey": "888888",
         "black": "000000", "white": "FFFFFF", "lime": "A4D65E", "navy": "0E3A6B"}
PALETTE = ["1A6BD7", "E6003E", "2EB82E", "F8C309", "9B59B6", "FE8402", "34D1ED", "DB70DB", "732112", "A4D65E", "0E9AA7", "888888"]


def _hex(v):
    t = str(v).strip().lstrip("#")
    t = NAMED.get(t.lower(), t)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", t):
        raise SSError(f"'{v}' is not a color. Use a name (red, green, blue, yellow, orange, purple, pink, cyan, teal, brown, gray, lime, navy) or 6-digit hex like 1A6BD7.")
    return t


def _select(d, ids, element_id, name_prefix, model=""):
    if not (ids or element_id or name_prefix or model):
        raise SSError("give ids, element_id, name_prefix or model so I know which elements to change")
    want = set(ids)
    out = []
    for e in d.get("elements", []):
        if want and e["id"] not in want and e.get("name") not in want and _a(e, 141) not in want:
            continue
        if element_id and e.get("element_id") != element_id:
            continue
        if name_prefix and not str(e.get("name", "")).startswith(name_prefix):
            continue
        if model and Q.norm(_a(e, 305)) != Q.norm(model):
            continue
        out.append(e)
    return out


def _lenses(e):
    raw = _a(e, 633)
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except Exception:
        return None


@mcp.tool()
@_guard
def set_colors(survey_id: str, ids: list[str] = [], element_id: int = 0, name_prefix: str = "", model: str = "",
               icon_color: str = "", fov_color: str = "", fov_transparency: float = -1, lens_colors: list[str] = [],
               color_by: str = "", palette: dict = {}, fov_matches_icon: bool = False, apply: bool = False) -> dict:
    """Change icon colors and field-of-view (coverage) colors. Only color/transparency values change; mount height, angle, direction, radius and everything else stay as surveyed.
    Pick elements with ids (uuid or name), element_id (type), name_prefix, or model. Colors: a name (red, green, blue, yellow, orange, purple, pink, cyan, teal, brown, gray, lime, navy) or 6-digit hex.
    icon_color = the element's icon; fov_color = single-lens coverage color; lens_colors = one color per lens on multi-lens cameras (in lens order; otherwise fov_color is used for every lens); fov_transparency 0-1.
    color_by = "model" | "type" | "status" | "system" gives every distinct value its own color automatically (palette {value: color} overrides); fov_matches_icon=true makes the coverage the same color as the icon.
    Dry run unless apply=true - ask the user which scheme they want first."""
    d = C.survey(survey_id)
    targets = _select(d, ids, element_id, name_prefix, model)
    if not targets:
        return {"error": "no matching elements"}
    if not (icon_color or fov_color or lens_colors or fov_transparency >= 0 or color_by):
        return {"error": "nothing to do: pass icon_color, fov_color, lens_colors, fov_transparency or color_by"}
    if fov_transparency > 1:
        return {"error": "fov_transparency is 0 (clear) to 1 (solid)"}
    pal = _pal()
    keyf = {"model": lambda e: _a(e, 305) or "(no model)", "type": lambda e: pal.get(e.get("element_id"), {}).get("name", "?"),
            "status": lambda e: _a(e, 138) or "(none)", "system": lambda e: pal.get(e.get("element_id"), {}).get("system", "?")}
    mapping = {}
    if color_by:
        if color_by not in keyf:
            return {"error": "color_by must be model, type, status or system"}
        keys = sorted({keyf[color_by](e) for e in targets})
        for i, k in enumerate(keys):
            mapping[k] = _hex(palette[k]) if k in palette else PALETTE[i % len(PALETTE)]
    changed, no_fov = [], 0
    for e in targets:
        before = _norm_attrs(e)
        ic = _hex(icon_color) if icon_color else None
        fc = _hex(fov_color) if fov_color else None
        if color_by:
            ic = mapping[keyf[color_by](e)]
            if fov_matches_icon:
                fc = ic
        elif fov_matches_icon and ic:
            fc = ic
        if ic:
            C.set_attr(e, 530, ic.lower())
        lens = _lenses(e)
        has_fov = bool(_a(e, 457)) or lens is not None
        if (fc or lens_colors or fov_transparency >= 0) and not has_fov:
            no_fov += 1
        if _a(e, 457) and fc:
            C.set_attr(e, 457, fc.upper())
        if _a(e, 458) is not None and fov_transparency >= 0 and _a(e, 457):
            C.set_attr(e, 458, fov_transparency)
        if lens is not None:
            for i, ln in enumerate(lens):
                c = _hex(lens_colors[i]) if i < len(lens_colors) else fc
                if c:
                    ln["color"] = c.upper()
                if fov_transparency >= 0:
                    ln["transparency"] = fov_transparency
            C.set_attr(e, 633, json.dumps(lens, separators=(",", ":")))
        if _norm_attrs(e) != before:
            changed.append(e)
    if not changed:
        return {"error": "nothing would change (already those colors, or the elements have no coverage area)", "elements_without_fov": no_fov}
    prev = [{"name": e.get("name"), "icon": _a(e, 530), "fov": _a(e, 457) or ([l.get("color") for l in (_lenses(e) or [])] or None)} for e in changed[:25]]
    info = {"count": len(changed), "elements_without_fov": no_fov, "color_map": mapping or None, "sample": prev}
    if not apply:
        return {"dry_run": True, **info}
    return _commit(d, "set_colors", info)


@mcp.tool()
@_guard
def set_attributes(survey_id: str, attributes: dict, ids: list[str] = [], element_id: int = 0, name_prefix: str = "", model: str = "", apply: bool = False) -> dict:
    """Set any attribute(s) on a group of elements: attributes {attr_id: value}, e.g. {"138": "Existing"} for status, {"167": "10"} for mount height, {"173": "Lobby camera"} for the description label.
    Pick elements with ids, element_id, name_prefix or model. The element ID (141) is blocked - use rename_elements. Shows old values per element. Dry run unless apply=true."""
    d = C.survey(survey_id)
    targets = _select(d, ids, element_id, name_prefix, model)
    attrs = {int(k): v for k, v in attributes.items()}
    if not attrs:
        return {"error": "attributes is empty"}
    if 141 in attrs or 140 in attrs:
        return {"error": "use rename_elements to change an element ID"}
    if not targets:
        return {"error": "no matching elements"}
    rows = []
    for e in targets:
        old = {C.attr_name(k): _a(e, k) for k in attrs}
        for k, v in attrs.items():
            C.set_attr(e, k, v)
        rows.append({"name": e.get("name"), "was": old})
    info = {"count": len(targets), "set": {C.attr_name(k): v for k, v in attrs.items()}}
    if not apply:
        return {"dry_run": True, **info, "elements": rows[:40]}
    return _commit(d, "set_attributes", info)


@mcp.tool()
@_guard
def survey_summary(survey_id: str) -> dict:
    """One-glance picture of a survey: counts by type and status, how many have models, equipment total, cable feet, labor hours, and whether this server may write to it."""
    d = C.survey(survey_id)
    pal = _pal()
    types = collections.Counter()
    status = collections.Counter()
    modeled = total = 0
    equip = hours = cable = 0.0
    for e in d.get("elements", []):
        q = Q.num(_a(e, 531), 1) or 1
        hours += (Q.num(_a(e, 533), 0) or 0) * q
        if e.get("element_id") == CABLE:
            cable += (Q.num(_a(e, 524), 0) or 0) + (Q.num(_a(e, 521), 0) or 0)
            continue
        total += 1
        types[pal.get(e.get("element_id"), {}).get("name", str(e.get("element_id")))] += 1
        status[_a(e, 138) or "(none)"] += 1
        if _a(e, 305):
            modeled += 1
        equip += (Q.num(_a(e, 532), 0) or 0) * q
    why = _may_write(d)
    return {"title": d.get("title"), "site": d.get("site"), "version": d.get("version"), "devices": total, "with_model": modeled,
            "without_model": total - modeled, "by_type": dict(types), "by_status": dict(status), "equipment_total_at_survey_prices": round(equip, 2),
            "cable_ft": round(cable, 1), "labor_hours": round(hours, 1), "claimed_for_editing_by": (d.get("editor") or {}).get("first_name") if isinstance(d.get("editor"), dict) else d.get("editor"), "writable_by_this_server": why is None, "why_not_writable": why}


@mcp.tool()
@_guard
def propose_placement(survey_id: str, items: list[dict]) -> dict:
    """Plan where a BOM goes on a survey and get the questions that need the user's answer. items: [{model|part, quantity, manufacturer?, description?}] (e.g. cpq_items or lines from salesforce-mcp cpq_read_quote).
    For each part: matches a saved preset; counts how many are already on the survey with that model; finds elements of the same type that have no model yet (the BOM part can go on these instead of adding new ones); and says how many still need a new spot.
    Returns per-part proposals plus a `questions` list - ASK THE USER those questions (use render_plan to show the plan and nearby_existing for context), then call apply_placement with their answers. Changes nothing."""
    d = C.survey(survey_id)
    profs = C.profiles()
    pal = _pal()
    els = [e for e in d.get("elements", []) if e.get("element_id") != CABLE]
    bom_models = set()
    parts, questions, unmatched = [], [], []
    for i in items:
        part = str(i.get("model") or i.get("part") or "").strip()
        if not part:
            continue
        qty = int(i.get("quantity") or i.get("qty") or 1)
        bom_models.add(Q.norm(part))
        p = Q.match_profile(profs, part, str(i.get("manufacturer") or ""))
        if not p:
            sug = find_products(part + " " + str(i.get("description") or ""), limit=4)
            unmatched.append({"part": part, "quantity": qty, "description": i.get("description"), "suggestions": sug if isinstance(sug, list) else []})
            questions.append(f"No saved preset matches {part} (x{qty}). Which element type / preset should it use, or skip it?")
            continue
        same_type = [e for e in els if e.get("element_id") == p["element_id"]]
        have = [e for e in same_type if Q.norm(_a(e, 305)) == Q.norm(part)]
        blank = sorted([e for e in same_type if not _a(e, 305)], key=lambda e: str(e.get("name")))
        need = max(qty - len(have), 0)
        assign = blank[:need]
        place = need - len(assign)
        tname = pal.get(p["element_id"], {}).get("name", str(p["element_id"]))
        row = {"part": part, "quantity": qty, "profile_id": p["id"], "preset": p["name"], "element_id": p["element_id"], "type": tname,
               "already_on_survey": len(have), "assign_to_existing": [{"id": e["id"], "name": e.get("name")} for e in assign],
               "needs_new_placement": place,
               "nearby_existing": [{"name": e.get("name"), "x": round((e.get("position") or {}).get("x", 0)), "y": round((e.get("position") or {}).get("y", 0))} for e in same_type[:12]]}
        parts.append(row)
        if len(have) > qty:
            questions.append(f"{part}: the BOM has {qty} but the survey already has {len(have)}. Is the BOM short, or should some be removed from the survey?")
        if assign:
            names = ", ".join(str(e.get("name")) for e in assign[:8]) + ("..." if len(assign) > 8 else "")
            questions.append(f"{part}: put it on the {len(assign)} {tname} element(s) with no model yet ({names})? Or are those for something else?")
        if place:
            questions.append(f"{part}: {place} more {tname} still need a spot. Where do they go (room/area, or point at a spot on render_plan)?")
    extra = collections.Counter(_a(e, 305) for e in els if _a(e, 305) and Q.norm(_a(e, 305)) not in bom_models)
    if extra:
        questions.append("The survey also has models that are not on this BOM: " + ", ".join(f"{m} x{n}" for m, n in extra.items()) + ". Is the BOM missing them, or is that expected?")
    return {"survey": d.get("title"), "parts": parts, "unmatched": unmatched, "survey_models_not_in_bom": dict(extra), "questions": questions,
            "next": "Ask the user the questions, then call apply_placement (dry run first) with assign=[{id, profile_id}] and place=[{profile_id, x, y, name?}]."}


@mcp.tool()
@_guard
def apply_placement(survey_id: str, assign: list[dict] = [], place: list[dict] = [], copy_specs: bool = False, apply: bool = False) -> dict:
    """Carry out the answers to propose_placement in ONE guarded save. assign: [{id, profile_id}] puts a preset's model/manufacturer/price/description on an existing element (its type must match the preset).
    place: [{profile_id, x, y, name?}] adds new elements at plan pixels. copy_specs=true also copies preset product specs (never surveyed geometry). Dry run unless apply=true; max 40 elements per save."""
    d = C.survey(survey_id)
    profs = {p["id"]: p for p in C.profiles()}
    pal = _pal()
    byid = {e["id"]: e for e in d.get("elements", [])}
    done = []
    for a in assign:
        e = byid.get(a.get("id"))
        p = profs.get(a.get("profile_id"))
        if not e or not p:
            return {"error": f"unknown element id or profile_id in assign: {a}"}
        if e.get("element_id") != p["element_id"]:
            return {"error": f"{e.get('name')} is a different element type than preset {p['name']}"}
        keep = {271, 305, 532, 173}
        for k, v in _pattrs(p).items():
            if v not in ("", None) and k not in SURVEYED and (copy_specs or k in keep):
                C.set_attr(e, k, v)
        e["element_profile_id"] = p["id"]
        done.append(e)
    for pl in place:
        p = profs.get(pl.get("profile_id"))
        if not p:
            return {"error": f"unknown profile_id in place: {pl}"}
        el = C.new_element(d, p["element_id"], pl["x"], pl["y"], pl.get("name"), {}, p)
        d["elements"].append(el)
        done.append(el)
    if not done:
        return {"error": "nothing to do: pass assign and/or place"}
    prev = [_brief(e, pal) for e in done]
    if not apply:
        return {"dry_run": True, "count": len(done), "assigned": len(assign), "added": len(place), "result": prev}
    return _commit(d, "apply_placement", {"count": len(done), "assigned": len(assign), "added": len(place), "result": prev})


# ---------------- Salesforce BOM -> survey ----------------
@mcp.tool()
@_guard
def load_bom(path: str) -> dict:
    """Read a Salesforce BOM/quote export (.csv or .xlsx). Columns are auto-detected (Product Code/Part Number/SKU/Model, Description, Quantity, Manufacturer, Unit/List Price)."""
    rows = Q.parse_bom(path)
    return {"rows": len(rows), "total_qty": sum(r["qty"] for r in rows), "items": rows}


def _match(rows):
    profs = C.profiles()
    matched, unmatched = [], []
    for r in rows:
        p = Q.match_profile(profs, r["part"], r.get("manufacturer", ""))
        if p:
            matched.append({**r, "profile_id": p["id"], "element_id": p["element_id"], "preset": p["name"]})
        else:
            sug = find_products(f'{r["part"]} {r.get("description", "")}', limit=4)
            unmatched.append({**r, "suggestions": sug if isinstance(sug, list) else []})
    return {"matched": matched, "unmatched": unmatched}


@mcp.tool()
@_guard
def match_parts(items: list[dict]) -> dict:
    """Match a parts list to saved System Surveyor presets. items: [{model|part, quantity|qty, manufacturer?, description?}] - e.g. lines from salesforce-mcp cpq_read_quote.
    Returns matched (profile_id + element_id, ready for place_elements) and unmatched with suggestions. Then ask the user where each part goes (render_plan)."""
    rows = [{"part": str(i.get("model") or i.get("part") or ""), "description": str(i.get("description") or ""),
             "qty": int(i.get("quantity") or i.get("qty") or 1), "manufacturer": str(i.get("manufacturer") or "")} for i in items]
    return _match([r for r in rows if r["part"]])


@mcp.tool()
@_guard
def match_bom(path: str) -> dict:
    """Match each Salesforce BOM line to a saved System Surveyor preset by model/part number.
    Returns matched (profile_id, element_id ready for place_elements) and unmatched lines with fuzzy suggestions.
    Next step: look at render_plan, ask the user where each part goes, then place_elements."""
    return _match(Q.parse_bom(path))


@mcp.tool()
@_guard
def render_plan(survey_id: str, grid: int = 100, labels: bool = True, highlight_missing_model: bool = False,
                proposed: list[dict] = [], scale: float = 1.5) -> dict:
    """Draw the floor plan with element markers + a coordinate grid to a PNG you can view (Read the returned path).
    proposed: [{x, y, label}] draws red crosses for planned placements so the user can confirm positions."""
    d = C.survey(survey_id)
    if not d.get("floorplan_url"):
        return {"error": "survey has no floor plan image"}
    out = OUT / f"plan_{survey_id}.png"
    r = P.render(d, out, client=C, grid=grid, labels=labels, scale=scale, highlight_missing_model=highlight_missing_model, extra_points=proposed)
    r["image_link"] = _link(out)
    return r


# ---------------- BOM + quote ----------------
def _cpq_items(q):
    return [{"model": l["model"], "manufacturer": l["manufacturer"], "description": l["type"], "quantity": l["qty"]}
            for l in q["lines"] if l["model"]]


@mcp.tool()
@_guard
def bom(survey_id: str) -> dict:
    """Bill of materials grouped by element type + manufacturer + model, with unit price where known and a list of gaps."""
    d = C.survey(survey_id)
    q = Q.build_quote(d, _pal(), C.profiles())
    return {"lines": [{k: l[k] for k in ("type", "manufacturer", "model", "qty", "unit_price")} for l in q["lines"]],
            "cpq_items": _cpq_items(q), "cable_ft": q["cable_ft"], "labor_hours": q["labor_hours"], "gaps": q["gaps"]}


@mcp.tool()
@_guard
def quote(survey_id: str, pricebook_path: str = "", labor_rate: float = DEF_LABOR, cable_per_ft: float = DEF_CABLE,
          markup_pct: float = DEF_MARKUP, tax_pct: float = DEF_TAX, out_dir: str = "") -> dict:
    """Price the survey into a quote. Unit price order: element price attr -> preset price -> pricebook file (Salesforce price export csv/xlsx).
    Cable = Cable Path length(+additional) x cable_per_ft; labor = Installation Hours x labor_rate.
    Defaults come from the LABOR_RATE, CABLE_PER_FT, MARKUP_PCT and TAX_PCT environment variables (labor_rate / cable_per_ft below 0 = leave unpriced). Equipment price is treated as cost and marked up by markup_pct (sell = cost x (1 + markup)). Pass other values to override. Writes <title>_quote.xlsx and a CSV of quote lines importable into a CRM/CPQ; returns totals + gaps."""
    d = C.survey(survey_id)
    pb = Q.load_pricebook(pricebook_path) if pricebook_path else {}
    lr = labor_rate if labor_rate >= 0 else None
    cf = cable_per_ft if cable_per_ft >= 0 else None
    q = Q.build_quote(d, _pal(), C.profiles(), pb, lr, cf, markup_pct, tax_pct)
    od = Path(out_dir) if (out_dir and not PUBLIC_URL) else OUT / "quotes"
    od.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"[^\w\-]+", "_", d.get("title") or survey_id)
    q["xlsx"] = Q.write_xlsx(q, od / f"{base}_quote.xlsx", lr, cf)
    q["salesforce_csv"] = Q.write_sf_csv(q, od / f"{base}_sf_lines.csv")
    q["xlsx_link"], q["salesforce_csv_link"] = _link(q["xlsx"]), _link(q["salesforce_csv"])
    q["cpq_items"] = _cpq_items(q)
    for l in q["lines"]:
        l["ids"] = len(l["ids"])
    return q


# ---------------- misc ----------------
@mcp.tool()
@_guard
def download_floorplan(survey_id: str, path: str = "") -> dict:
    """Save the raw floor-plan image to disk and return the path."""
    d = C.survey(survey_id)
    url = d.get("floorplan_url") or d.get("preview_image")
    if not url:
        return {"error": "survey has no floor plan image"}
    r = httpx.get(url, follow_redirects=True, timeout=60)
    r.raise_for_status()
    ext = "png" if "png" in r.headers.get("content-type", "") else "jpg"
    p = Path(path or (OUT / f"floorplan_{survey_id}.{ext}"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    return {"path": str(p), "bytes": len(r.content), "scale": d.get("floorplan_scale")}


@mcp.tool()
@_guard
def raw_get(path: str) -> dict:
    """Read-only GET against /v3 (e.g. /user). For exploring the API."""
    return C.get(path if path.startswith("/") else "/" + path)


@mcp.tool()
@_guard
def status() -> dict:
    """Health and safety settings of this server: login/keepalive state, whether writes are on, who may be written, limits. Call this first if anything looks wrong."""
    return {"logged_in": bool(C.tok.get("refreshToken")), "keepalive": _ka_view(), "writes_enabled": WRITES,
            "writes_allowed_for": f"surveys created by user {OWNER_USER_ID} on team {C.team_id()}, plus allowlist {sorted(WRITE_SURVEYS)}",
            "max_elements_per_save": MAX_ELEMENTS, "backups": len(list((OUT / "backups").glob("*.json"))) if (OUT / "backups").exists() else 0}


@mcp.tool()
@_guard
def list_backups(survey_id: str = "", limit: int = 20) -> dict:
    """Snapshots taken automatically before every save (newest first), plus the recent change journal. Use restore_backup to roll one back."""
    bk = OUT / "backups"
    files = sorted(bk.glob(f"{survey_id or ''}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit] if bk.exists() else []
    jr = []
    try:
        jr = [json.loads(x) for x in (OUT / "journal.jsonl").read_text().splitlines()[-limit:]][::-1]
        if survey_id:
            jr = [j for j in jr if j.get("survey") == survey_id]
    except Exception:
        pass
    return {"backups": [{"file": p.name, "kb": p.stat().st_size // 1024} for p in files], "journal": jr}


@mcp.tool()
@_guard
def restore_backup(survey_id: str, backup: str, apply: bool = False) -> dict:
    """Roll a survey back to a snapshot from list_backups. Dry run shows what would change; apply=true restores (and snapshots the current state first, so a restore can itself be undone)."""
    p = OUT / "backups" / Path(backup).name
    if not p.is_file() or not p.name.startswith(survey_id):
        return {"error": "backup not found for that survey (use list_backups)"}
    snap = json.loads(p.read_text())
    live = C.survey(survey_id)
    a, b = {e["id"]: e for e in live.get("elements", [])}, {e["id"]: e for e in snap.get("elements", [])}
    changed = [i for i in a if i in b and _norm_attrs(a[i]) != _norm_attrs(b[i])]
    summary = {"would_remove": [a[i].get("name") for i in a if i not in b], "would_re_add": [b[i].get("name") for i in b if i not in a],
               "would_revert_attributes_on": [a[i].get("name") for i in changed]}
    if not apply:
        return {"dry_run": True, **summary}
    snap["version"] = live.get("version")
    return _commit(snap, "restore_backup", summary, force_count=len(changed) + len(summary["would_remove"]) + len(summary["would_re_add"]))


# ---------------- HTTP server ----------------
KA_FILE = OUT / "keepalive.json"
KA_HOURS = float(os.environ.get("KEEPALIVE_HOURS", 2))
HA_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")
_ka = {"last_ok": 0, "last_try": 0, "fails": 0, "auth_dead": False, "alerted": 0, "last_error": ""}
try:
    _ka.update(json.loads(KA_FILE.read_text()))
except Exception:
    pass


def _ka_save():
    try:
        KA_FILE.write_text(json.dumps(_ka))
    except Exception:
        pass


def _ka_view():
    return {"token_ok": _ka["fails"] == 0 and bool(_ka["last_ok"]), "last_refresh_minutes_ago": round((time.time() - _ka["last_ok"]) / 60) if _ka["last_ok"] else None,
            "consecutive_failures": _ka["fails"], "needs_human_login": _ka["auth_dead"], "last_error": _ka["last_error"], "refresh_every_hours": KA_HOURS}


def _alert(subject, desc, importance="alert"):
    """POST a JSON alert to ALERT_WEBHOOK (optional). Payload: {event, importance, subject, description}; adapt to Slack/ntfy/Home Assistant/etc."""
    if not HA_WEBHOOK:
        return
    try:
        import httpx
        httpx.post(HA_WEBHOOK, json={"event": "System Surveyor MCP", "importance": importance, "subject": subject, "description": desc}, timeout=10)
    except Exception as e:
        print("alert failed", e, flush=True)


def _ka_once():
    """One refresh attempt (3 tries for network blips). Returns True when the login is healthy."""
    _ka["last_try"] = time.time()
    for wait in (0, 60, 300):
        time.sleep(wait)
        try:
            C.refresh()
            was = _ka["alerted"]
            _ka.update(last_ok=time.time(), fails=0, auth_dead=False, last_error="", alerted=0)
            _ka_save()
            if was:
                _alert("System Surveyor login is back", "The login is healthy again.", "normal")
            return True
        except SSAuthError as e:
            _ka.update(auth_dead=True, last_error=str(e))
            break
        except Exception as e:
            _ka["last_error"] = str(e)
    _ka["fails"] += 1
    now = time.time()
    if _ka["auth_dead"] and now - _ka["alerted"] > 24 * 3600:
        _ka["alerted"] = now
        _alert("System Surveyor login expired", "The saved login was rejected and cannot fix itself (it needs a captcha). On the PC run: python login.py then push_token.sh in systemsurveyor-mcp. The container picks it up on its own within 10 minutes.")
    elif not _ka["auth_dead"] and _ka["fails"] >= 3 and now - _ka["alerted"] > 24 * 3600:
        _ka["alerted"] = now
        _alert("System Surveyor keepalive failing", f"{_ka['fails']} refreshes in a row failed: {_ka['last_error']}")
    _ka_save()
    return False


def _keepalive():
    """Refresh the rotating 7-day token every KEEPALIVE_HOURS so it never lapses; poll every 10 min while it is broken so a re-login is picked up fast."""
    while True:
        ok = _ka_once()
        print("keepalive", "ok" if ok else f"FAILED {_ka['last_error']}", flush=True)
        time.sleep(KA_HOURS * 3600 if ok else 600)


def _find_file(name):
    for base in (OUT, OUT / "quotes"):
        p = base / Path(name).name
        if p.is_file():
            return p


async def _health(request):
    from starlette.responses import JSONResponse
    v = _ka_view()
    bad = _ka["fails"] >= 3 or _ka["auth_dead"]
    return JSONResponse({"ok": not bad, "writes": WRITES, "logged_in": bool(C.tok.get("refreshToken")), **v}, status_code=503 if bad else 200)


async def _download(request):
    from starlette.responses import FileResponse, PlainTextResponse
    name = request.path_params["name"]
    exp, sig = request.query_params.get("exp", "0"), request.query_params.get("sig", "")
    want = hmac.new(MCP_TOKEN.encode(), f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    p = _find_file(name)
    if not p or not hmac.compare_digest(sig, want) or int(exp or 0) < time.time():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p)


class _Auth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            h = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not (MCP_TOKEN and hmac.compare_digest(h, f"Bearer {MCP_TOKEN}")):
                from starlette.responses import PlainTextResponse
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app():
    from starlette.routing import Route
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/health", _health))
    app.router.routes.insert(0, Route("/dl/{name}", _download))
    return _Auth(app)


if __name__ == "__main__":
    import sys
    if os.environ.get("MCP_TRANSPORT") == "http" or "--http" in sys.argv:
        if not MCP_TOKEN:
            sys.exit("MCP_TOKEN is required in http mode")
        import uvicorn
        threading.Thread(target=_keepalive, daemon=True).start()
        uvicorn.run(http_app(), host="0.0.0.0", port=mcp.settings.port, log_level="warning")
    else:
        mcp.run()
