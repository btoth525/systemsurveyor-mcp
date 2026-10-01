"""System Surveyor MCP server. Writes are dry-run unless apply=true."""
import base64, collections, concurrent.futures, copy, csv, datetime, functools, hashlib, hmac, io, json, math, os, re, sys, threading, time, uuid
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
Looking things up: find_elements (search/filter a survey), get_element (every attribute of one), bom_diff (a Salesforce BOM vs what the survey says, by model), export_elements (equipment schedule CSV). Elements can be named by uuid or by ID like FCAM-001; an unknown or duplicated name is an error, never a guess. Cable paths are never moved or deleted by accident: move_elements refuses lines, delete_elements refuses to leave a cable dangling. The server itself counts what a save really changes and refuses oversized saves, and refuses any save that removes elements unless it is delete_elements or restore_backup.
Survey-level tools: rename_survey (title; read the exact Salesforce opportunity name first), duplicate_survey (server-side copy for versioning), replace_floorplan (new background image, elements kept; image must be the same pixel size), survey_diff (A vs B, or vs a 'backup:<file>'), recent_changes (which surveys changed since a time), copy_elements (between the user's own surveys), add_cable_path / set_cable_path (cable runs with type and length), import_price_book (CSV prices into ONE survey - ask whether they are cost or list, because quote treats survey prices as cost and adds markup), export_quote_pdf (a DRAFT-stamped customer PDF; the real quote is Salesforce CPQ). my_parts keeps a personal parts list; the team's shared presets are never written by this server because they belong to everyone.
Survey -> Salesforce recipe (Salesforce stays read-only here): the user names the opportunity -> salesforce_query for its exact name -> rename_survey (dry run, yes, apply) -> bom -> cpq_match_parts -> cpq_preview_changes -> user confirms -> cpq_add_lines. Use Salesforce CPQ list/sell prices for the quote, not the survey's device price.
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
HTTP_MODE = os.environ.get("MCP_TRANSPORT") == "http" or "--http" in sys.argv
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


def _pos(e):
    return json.dumps(e.get("position"), sort_keys=True)


def _diff(old, new):
    """What a save would actually change, computed from the documents themselves (not from what a tool says it did)."""
    a, b = {e["id"]: e for e in old.get("elements", [])}, {e["id"]: e for e in new.get("elements", [])}
    changed = [i for i in b if i in a and (_norm_attrs(a[i]) != _norm_attrs(b[i]) or _pos(a[i]) != _pos(b[i])
                                           or a[i].get("element_profile_id") != b[i].get("element_profile_id"))]
    return {"changed": changed, "added": [i for i in b if i not in a], "removed": [i for i in a if i not in b]}


def _meta(old, new):
    """Survey-level fields a tool may change (just the title)."""
    return [k for k in ("title",) if new.get(k) is not None and (old.get(k) or "") != (new.get(k) or "")]


def _gate(live):
    """Why a write that does not go through _commit (floor plan swap) must not happen, or None."""
    if not WRITES:
        return "Writes are switched off on this server (ALLOW_WRITES=false). Nothing was changed."
    why = _may_write(live)
    if why:
        return why
    ed = live.get("editor")
    ed = ed.get("user_id") if isinstance(ed, dict) else ed
    if ed not in (None, "", 0) and OWNER_USER_ID and ed != OWNER_USER_ID:
        return f"someone else (user {ed}) has this survey claimed for editing. Nothing changed."
    return None


def _journal(tool, survey, title, **kw):
    with open(OUT / "journal.jsonl", "a") as f:
        f.write(json.dumps({"t": time.strftime("%Y%m%d-%H%M%S"), "tool": tool, "survey": survey, "title": title, **kw}) + "\n")


def _nm(doc, i):
    for e in doc.get("elements", []):
        if e["id"] == i:
            return e.get("name")
    return i


def _verify(d, live):
    """Re-read after saving: every element we meant to save must be there with the same attributes."""
    want = {e["id"]: e for e in d.get("elements", [])}
    got = {e["id"]: e for e in live.get("elements", [])}
    bad = [i for i in want if i not in got] + [i for i in got if i not in want]
    for i, e in want.items():
        if i in got and (_norm_attrs(e) != _norm_attrs(got[i]) or _pos(e) != _pos(got[i])):
            bad.append(i)
    if d.get("title") is not None and (live.get("title") or "") != (d.get("title") or ""):
        bad.append("title")
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
        df = _diff(live, d)
        meta = _meta(live, d)
        actual = len(df["changed"]) + len(df["added"]) + len(df["removed"]) + len(meta)
        n = max(n, actual)
        if actual == 0:
            return {"saved": False, "note": "nothing would change (already as requested). No save was made."}
        if n > MAX_ELEMENTS:
            return {"saved": False, "blocked": f"{n} elements would change, over the limit of {MAX_ELEMENTS} per save. Split the change into smaller batches."}
        if df["removed"] and tool not in ("delete_elements", "restore_backup"):
            return {"saved": False, "blocked": f"{tool} would remove {len(df['removed'])} element(s), which it must never do. Nothing changed."}
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
            f.write(json.dumps({"t": stamp, "tool": tool, "survey": d["id"], "title": d.get("title"), "n": n, "verified": ver["ok"],
                                "changed": [_nm(live, i) for i in df["changed"]][:60], "added": len(df["added"]), "removed": [_nm(live, i) for i in df["removed"]][:60], "meta": meta}) + "\n")
        out = {"saved": True, **extra, "actual_changes": {**{k: len(v) for k, v in df.items()}, **({"title": 1} if meta else {})}, "backup": f"{d['id']}-{stamp}.json", "verified": ver}
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
        except httpx.HTTPError as e:
            return {"error": f"network problem talking to a remote service ({type(e).__name__}). Nothing was changed; try again."}
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
            return {"error": f"bad input ({type(e).__name__}: {e}). Nothing was changed. Check the argument names and types in the tool description."}
    return w


def _resolve(d, refs):
    """Elements for a list of uuids / names / IDs. Unknown or ambiguous references are an error, never silently skipped."""
    out, seen = [], set()
    els = d.get("elements", [])
    for r in refs:
        hits = [e for e in els if e["id"] == r] or [e for e in els if r in (e.get("name"), _a(e, 141))]
        if not hits:
            raise SSError(f"element '{r}' not found in this survey")
        if len(hits) > 1:
            raise SSError(f"'{r}' matches {len(hits)} elements (duplicate names); use the uuid from find_elements")
        if hits[0]["id"] not in seen:
            seen.add(hits[0]["id"])
            out.append(hits[0])
    return out


def _xy(m):
    try:
        x, y = float(m["x"]), float(m["y"])
    except (KeyError, TypeError, ValueError):
        raise SSError(f"each location needs numeric x and y (plan pixels), got {m}")
    if not (math.isfinite(x) and math.isfinite(y)) or not (0 <= x <= 50000 and 0 <= y <= 50000):
        raise SSError(f"x/y out of range: {x}, {y} (plan pixels, 0..50000)")
    return round(x, 2), round(y, 2)


def _confine(path):
    """In HTTP (container) mode the AI may only read/write under the data folder."""
    p = Path(path)
    if not HTTP_MODE:
        return p
    try:
        q = (p if p.is_absolute() else OUT / p).resolve()
        q.relative_to(OUT.resolve())
    except Exception:
        raise SSError(f"in server mode files must be inside {OUT}")
    return q


def _cables_touching(d, idset):
    return [e for e in d.get("elements", []) if e.get("element_id") == CABLE and e["id"] not in idset
            and any(((e.get("connections") or {}).get(side) or {}).get("id") in idset for side in ("start", "end"))]


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
    return [{"id": s["id"], "title": s["title"], "status": s.get("status"), "elements": s.get("element_count"), "modified": s.get("modified_at"),
             "claimed_for_editing_by": (s.get("editor") or {}).get("first_name") if isinstance(s.get("editor"), dict) else s.get("editor")}
            for s in C.surveys(site_id)]


@mcp.tool()
@_guard
def get_survey(survey_id: str, elements: bool = True, limit: int = 300, offset: int = 0) -> dict:
    """Survey summary plus a compact list of elements (id, name, type, x/y plan pixels, status, manufacturer, model, price). `truncated` is true when there are more than limit (page with offset, or use find_elements)."""
    d = C.survey(survey_id)
    pal = _pal()
    out = {k: d.get(k) for k in ("id", "title", "unit", "floorplan_scale", "icon_size", "version", "modified_at", "modified_source", "summary")}
    out["site"] = d.get("site")
    els = d.get("elements", [])
    out["element_count"] = len(els)
    if elements:
        out["elements"] = [_brief(e, pal) for e in els[offset:offset + limit]]
        out["truncated"] = offset + limit < len(els)
    out["editor"] = d.get("editor")
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
    for m in _mine_load():
        if element_id and m.get("element_id") and m["element_id"] != element_id:
            continue
        hay = f'{m["manufacturer"]} {m["model"]} {m.get("description", "")}'.lower()
        s = sum(1 for t in toks if t in hay) + (10 if nq and Q.norm(m["model"]) == nq else 0)
        if s:
            scored.append((s, {"profile_id": None, "source": "my_parts", "name": m["model"], "element_id": m.get("element_id") or None,
                               "manufacturer": m["manufacturer"], "model": m["model"], "price": m.get("price"), "description": m.get("description", "")}))
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
    cand = _select(d, ids, element_id, name_prefix)
    skipped = {"wrong_type_for_preset": 0, "already_has_model": 0}
    targets = []
    for e in cand:
        if prof and e.get("element_id") != prof["element_id"]:
            skipped["wrong_type_for_preset"] += 1
            continue
        if only_missing and _a(e, 305):
            skipped["already_has_model"] += 1
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
    if 141 in vals or 140 in vals:
        return {"error": "the element ID/name (141/140) cannot be set here; use rename_elements"}
    if not vals:
        return {"error": "nothing to set: pass profile_id or model/manufacturer/price/description/attributes"}
    if not targets:
        return {"error": "no matching elements (check only_missing / filters)", "skipped": skipped}
    pal = _pal()
    for e in targets:
        for k, v in vals.items():
            C.set_attr(e, k, v)
        if prof:
            e["element_profile_id"] = prof["id"]
    preview = [_brief(e, pal) for e in targets]
    if not apply:
        return {"dry_run": True, "count": len(targets), "skipped": skipped, "set": {C.attr_name(k): v for k, v in vals.items()}, "would_update": preview}
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
        if int(eid) == CABLE:
            return {"error": "cable paths are lines between devices; draw them in the web app. Place devices here."}
        x, y = _xy(i)
        if i.get("name") and any(i["name"] in (e.get("name"), _a(e, 141)) for e in d["elements"]):
            return {"error": f"the name {i['name']} is already used in this survey"}
        at = {int(k): v for k, v in (i.get("attributes") or {}).items()}
        for key, aid in (("manufacturer", 271), ("model", 305), ("price", 532), ("description", 173)):
            if i.get(key) not in (None, ""):
                at[aid] = i[key]
        if 141 in at or 140 in at:
            return {"error": "set the name with the name field, not attributes 141/140"}
        el = C.new_element(d, eid, x, y, i.get("name"), at, prof)
        d["elements"].append(el)
        new.append(el)
    if not new:
        return {"error": "items is empty"}
    preview = [_brief(e, _pal()) for e in new]
    if not apply:
        return {"dry_run": True, "would_add": preview}
    return _commit(d, "place_elements", {"added": preview})


@mcp.tool()
@_guard
def move_elements(survey_id: str, moves: list[dict], apply: bool = False) -> dict:
    """Move devices on the plan. moves: [{id: uuid or name like FCAM-001, x, y}] in plan pixels (render_plan shows the grid). Cable paths (lines) cannot be moved here. Shows from -> to for each. Dry run unless apply=true."""
    d = C.survey(survey_id)
    if not moves:
        return {"error": "moves is empty"}
    refs = [m.get("id") for m in moves]
    if len(set(refs)) != len(refs):
        return {"error": "an element appears twice in moves"}
    rows = []
    for m, e in zip(moves, _resolve(d, refs)):
        p = e.get("position")
        if not isinstance(p, dict):
            return {"error": f"{e.get('name')} is a line/cable path; moving it here would corrupt it. Edit it in the web app."}
        x, y = _xy(m)
        rows.append({"id": e["id"], "name": e.get("name"), "from": [round(p["x"], 1), round(p["y"], 1)], "to": [x, y]})
        e["position"] = {"x": x, "y": y}
    touching = _cables_touching(d, {r["id"] for r in rows})
    info = {"moves": len(rows), "rows": rows}
    if touching:
        info["note"] = "cable paths attached to these devices keep their drawn route: " + ", ".join(str(c.get("name")) for c in touching[:12])
    if not apply:
        return {"dry_run": True, **info}
    return _commit(d, "move_elements", info)


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
def delete_elements(survey_id: str, ids: list[str], include_attached_cables: bool = False, apply: bool = False) -> dict:
    """Delete elements (uuid or name). ONLY when the user explicitly asked. Refuses if a cable path is attached to a device being deleted, unless those paths are listed too or include_attached_cables=true (so no cable is left dangling). Lists exactly what goes. Dry run unless apply=true; restore_backup undoes it."""
    d = C.survey(survey_id)
    if not ids:
        return {"error": "ids is empty"}
    gone = _resolve(d, ids)
    idset = {e["id"] for e in gone}
    touching = _cables_touching(d, idset)
    if touching and not include_attached_cables:
        return {"error": "these cable paths are attached to devices you are deleting and would be left dangling: " + ", ".join(str(c.get("name")) for c in touching)
                + ". Add them to ids, or pass include_attached_cables=true."}
    for c in touching:
        gone.append(c)
        idset.add(c["id"])
    d["elements"] = [e for e in d["elements"] if e["id"] not in idset]
    pal = _pal()
    rows = [_brief(e, pal) for e in gone]
    if not apply:
        return {"dry_run": True, "would_delete": len(rows), "elements": rows}
    return _commit(d, "delete_elements", {"deleted": len(rows), "elements": rows})


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
    byref = {e["id"] for e in _resolve(d, ids)} if ids else None
    out = []
    for e in d.get("elements", []):
        if byref is not None and e["id"] not in byref:
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
    done = []
    if len({a.get("id") for a in assign}) != len(assign):
        return {"error": "an element appears twice in assign"}
    for a in assign:
        e = _resolve(d, [a.get("id")])[0]
        p = profs.get(a.get("profile_id"))
        if not p:
            return {"error": f"unknown profile_id in assign: {a}"}
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
        x, y = _xy(pl)
        if pl.get("name") and any(pl["name"] in (e.get("name"), _a(e, 141)) for e in d["elements"]):
            return {"error": f"the name {pl['name']} is already used in this survey"}
        el = C.new_element(d, p["element_id"], x, y, pl.get("name"), {}, p)
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
    rows = Q.parse_bom(str(_confine(path)))
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
    return _match(Q.parse_bom(str(_confine(path))))


@mcp.tool()
@_guard
def render_plan(survey_id: str, grid: int = 100, labels: bool = True, highlight_missing_model: bool = False,
                proposed: list[dict] = [], scale: float = 1.5, icon_colors: bool = True) -> dict:
    """Draw the floor plan with element markers + a coordinate grid to a PNG you can view (Read the returned path).
    proposed: [{x, y, label}] draws red crosses for planned placements so the user can confirm positions. icon_colors=true draws each marker in the element's real icon color (set_colors), so a recolor can be checked visually."""
    d = C.survey(survey_id)
    if not d.get("floorplan_url"):
        return {"error": "survey has no floor plan image"}
    scale = min(max(float(scale), 0.3), 3.0)
    grid = 0 if not grid else min(max(int(grid), 20), 1000)
    out = OUT / f"plan_{survey_id}.png"
    r = P.render(d, out, client=C, grid=grid, labels=labels, scale=scale, highlight_missing_model=highlight_missing_model, extra_points=proposed, icon_colors=icon_colors)
    r["image_link"] = _link(out)
    return r


# ---------------- find / inspect / reconcile / export ----------------
@mcp.tool()
@_guard
def find_elements(survey_id: str, query: str = "", element_id: int = 0, status: str = "", model: str = "", missing_model: bool = False,
                  limit: int = 50, offset: int = 0) -> dict:
    """Search a survey's elements. query matches name, type, manufacturer, model and label text; filter by element_id (type), status, model, or missing_model=true. Returns ids you can pass to the change tools. Read-only."""
    d = C.survey(survey_id)
    pal = _pal()
    out = []
    for e in d.get("elements", []):
        if element_id and e.get("element_id") != element_id:
            continue
        if status and (_a(e, 138) or "").lower() != status.lower():
            continue
        if model and Q.norm(model) not in Q.norm(_a(e, 305)):
            continue
        if missing_model and _a(e, 305):
            continue
        row = _brief(e, pal)
        hay = " ".join(str(row.get(k) or "") for k in ("name", "type", "manufacturer", "model", "label", "status")).lower()
        if query and query.lower() not in hay:
            continue
        out.append(row)
    return {"total": len(out), "elements": out[offset:offset + limit], "truncated": offset + limit < len(out)}


@mcp.tool()
@_guard
def get_element(survey_id: str, id: str) -> dict:
    """Everything stored on one element (uuid or name like FCAM-001): every attribute with its name, position, preset, cable connections. Use it to see exactly what is set before changing something. Read-only."""
    d = C.survey(survey_id)
    e = _resolve(d, [id])[0]
    return {"id": e["id"], "name": e.get("name"), "type": _pal().get(e.get("element_id"), {}).get("name", e.get("element_id")),
            "element_id": e.get("element_id"), "position": e.get("position"), "element_profile_id": e.get("element_profile_id"),
            "connections": e.get("connections") or None, "accessories": len(e.get("accessories") or []),
            "attributes": {f"{a['attribute_id']} {C.attr_name(a['attribute_id'])}": a.get("value") for a in e.get("attributes", []) if a.get("value") not in ("", None)}}


@mcp.tool()
@_guard
def bom_diff(survey_id: str, items: list[dict]) -> dict:
    """Compare a BOM (e.g. cpq_items / lines from a Salesforce quote: [{model|part, quantity}]) with what the survey says, by model number. Returns short (BOM wants more than the survey shows), over (survey shows more), only_in_bom, only_in_survey, and how many survey devices still have no model. Use this to reconcile a quote against a survey. Read-only."""
    d = C.survey(survey_id)
    q = Q.build_quote(d, _pal(), C.profiles())
    have, label = collections.Counter(), {}
    for l in q["lines"]:
        if l["model"]:
            have[Q.norm(l["model"])] += l["qty"]
            label[Q.norm(l["model"])] = l["model"]
    want = collections.Counter()
    for i in items:
        part = str(i.get("model") or i.get("part") or "").strip()
        if part:
            want[Q.norm(part)] += int(i.get("quantity") or i.get("qty") or 1)
            label.setdefault(Q.norm(part), part)
    short = [{"part": label[k], "bom": want[k], "survey": have.get(k, 0), "short_by": want[k] - have.get(k, 0)} for k in want if k in have and want[k] > have[k]]
    over = [{"part": label[k], "bom": want[k], "survey": have[k], "over_by": have[k] - want[k]} for k in want if k in have and have[k] > want[k]]
    unmodeled = sum(1 for e in d.get("elements", []) if e.get("element_id") != CABLE and not _a(e, 305))
    return {"matches": sum(1 for k in want if have.get(k) == want[k]), "short": short, "over": over,
            "only_in_bom": [{"part": label[k], "quantity": want[k]} for k in want if k not in have],
            "only_in_survey": [{"part": label[k], "quantity": have[k]} for k in have if k not in want],
            "survey_devices_without_model": unmodeled}


@mcp.tool()
@_guard
def export_elements(survey_id: str) -> dict:
    """Write an equipment schedule CSV of every element (ID, type, status, manufacturer, model, price, qty, hours, label, mount height, icon color, x, y) and return a download link. Read-only."""
    d = C.survey(survey_id)
    pal = _pal()
    od = OUT / "quotes"
    od.mkdir(parents=True, exist_ok=True)
    p = od / (re.sub(r"[^\w\-]+", "_", d.get("title") or survey_id) + "_elements.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Type", "Status", "Manufacturer", "Model", "Price", "Qty", "Install hours", "Label", "Mount height", "Icon color", "X", "Y", "UUID"])
        for e in d.get("elements", []):
            pos = e.get("position") if isinstance(e.get("position"), dict) else {}
            w.writerow([e.get("name"), pal.get(e.get("element_id"), {}).get("name", e.get("element_id")), _a(e, 138), _a(e, 271), _a(e, 305), _a(e, 532),
                        _a(e, 531), _a(e, 533), _a(e, 173), _a(e, 167), _a(e, 530), round(pos.get("x", 0), 1) if pos else "", round(pos.get("y", 0), 1) if pos else "", e["id"]])
    return {"rows": len(d.get("elements", [])), "path": str(p), "link": _link(p)}


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
    pb = Q.load_pricebook(str(_confine(pricebook_path))) if pricebook_path else {}
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


# ---------------- personal parts catalog (never touches shared team presets) ----------------
_MINE = OUT / "my_parts.json"
_mlock = threading.Lock()


def _mine_load():
    try:
        return json.loads(_MINE.read_text()).get("parts", [])
    except Exception:
        return []


@mcp.tool()
@_guard
def my_parts(action: str = "list", manufacturer: str = "", model: str = "", price: float = -1, description: str = "", element_id: int = 0) -> dict:
    """The user's PERSONAL parts catalog on this server (manufacturer, model, price) - a place to keep matched parts for reuse. It does NOT change the team's shared System Surveyor presets (those belong to everyone, so this server never writes them). Parts saved here show up in find_products (source "my_parts") and can be applied to a survey with assign_models(manufacturer=, model=, price=).
    action: "list", "save" (manufacturer + model required; price/description/element_id optional; same model replaces the old row) or "delete" (by model). Only touches a small file on the server, never a survey."""
    act = action.lower()
    with _mlock:
        parts = _mine_load()
        if act == "list":
            return {"count": len(parts), "parts": parts}
        if not model.strip():
            return {"error": "model is required"}
        key = Q.norm(model)
        if act == "delete":
            keep = [p for p in parts if Q.norm(p["model"]) != key]
            if len(keep) == len(parts):
                return {"error": f"no saved part with model {model}"}
            _MINE.write_text(json.dumps({"parts": keep}, indent=1))
            return {"deleted": model, "count": len(keep)}
        if act != "save":
            return {"error": "action must be list, save or delete"}
        if not manufacturer.strip():
            return {"error": "manufacturer is required"}
        if price != -1 and (not math.isfinite(price) or price < 0 or price > 1_000_000):
            return {"error": "price must be between 0 and 1,000,000 (leave it out if unknown)"}
        if len(parts) >= 2000 and not any(Q.norm(p["model"]) == key for p in parts):
            return {"error": "personal catalog is full (2000 parts)"}
        row = {"manufacturer": manufacturer.strip()[:80], "model": model.strip()[:80], "price": None if price == -1 else round(price, 2),
               "description": description.strip()[:300], "element_id": element_id or None, "saved": time.strftime("%Y-%m-%d")}
        parts = [p for p in parts if Q.norm(p["model"]) != key] + [row]
        _MINE.write_text(json.dumps({"parts": parts}, indent=1))
        return {"saved": row, "count": len(parts)}


# ---------------- survey-level: rename / duplicate / floor plan / diff / changes ----------------
def _title_ok(t):
    t = (t or "").strip()
    if not t or len(t) > 200 or any(ord(c) < 32 for c in t):
        raise SSError("title must be 1-200 characters with no control characters")
    return t


@mcp.tool()
@_guard
def rename_survey(survey_id: str, title: str, apply: bool = False) -> dict:
    """Change a survey's title (e.g. to match the Salesforce opportunity name - read the exact name from Salesforce first, never type it from memory). Own surveys only. Dry run unless apply=true; restore_backup undoes it."""
    d = C.survey(survey_id)
    new = _title_ok(title)
    info = {"title_was": d.get("title"), "title_will_be": new}
    if (d.get("title") or "") == new:
        return {"note": "the survey already has that title", **info}
    d["title"] = new
    if not apply:
        return {"dry_run": True, **info}
    return _commit(d, "rename_survey", info, force_count=1)


@mcp.tool()
@_guard
def duplicate_survey(survey_id: str, new_title: str = "", apply: bool = False) -> dict:
    """Make a copy of a survey (same site, same floor plan, all elements) for versioning before edits, using System Surveyor's own copy function. The original is never touched. The copy is a new survey owned by the user, so it can be edited like any of their own surveys. new_title defaults to '<title> - copy <date>'. Dry run unless apply=true."""
    d = C.survey(survey_id)
    if d.get("team_id") != C.team_id():
        return {"error": f"that survey belongs to team {d.get('team_id')}, not this server's team; it will not be copied"}
    site_id = d["site"]["id"] if isinstance(d.get("site"), dict) else d.get("site")
    title = _title_ok(new_title) if new_title.strip() else f"{d.get('title')} - copy {time.strftime('%Y-%m-%d')}"[:200]
    info = {"source": d.get("title"), "elements": len(d.get("elements", [])), "new_title": title, "same_site": site_id}
    if not apply:
        return {"dry_run": True, **info, "note": "creates one new survey in the same site; the original is not changed"}
    if not WRITES:
        return {"saved": False, "blocked": "Writes are switched off on this server (ALLOW_WRITES=false). Nothing was created."}
    with _wlock:
        before = {x["id"] for x in C.surveys(site_id)}
        job = C.copy_survey(survey_id)
        if not job:
            return {"error": "System Surveyor did not start a copy job; nothing was created (check the site for a stray copy)"}
        res = {}
        for _ in range(60):
            time.sleep(2)
            res = C.copy_status(survey_id, job)
            st = str(res.get("status") or "").lower()
            if st in ("completed", "complete", "success", "succeeded", "done"):
                break
            if st in ("failed", "error", "errored"):
                return {"error": f"System Surveyor's copy failed: {res.get('message') or res}"}
        else:
            return {"error": "the copy is still running after 2 minutes; check the site for the new survey before trying again (so you do not make two)"}
        new = [x for x in C.surveys(site_id) if x["id"] not in before]
        if len(new) != 1:
            return {"error": f"copy finished but {len(new)} new surveys appeared in the site, so I will not guess which is yours: {[(x['id'], x.get('title')) for x in new]}"}
    nid = new[0]["id"]
    _journal("duplicate_survey", nid, title, copied_from=survey_id, n=len(d.get("elements", [])))
    out = {"saved": True, "new_survey_id": nid, "copied_from": survey_id, **info}
    nd = C.survey(nid)
    if (nd.get("title") or "") != title:
        was = nd.get("title")
        nd["title"] = title
        r = _commit(nd, "rename_survey", {"title_was": was, "title_will_be": title}, force_count=1)
        out["rename"] = {k: r.get(k) for k in ("saved", "blocked", "error", "verified") if k in r}
    out["elements_in_copy"] = len(C.survey(nid).get("elements", []))
    return out


def _img_info(b):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(b))
        w, h = im.size
        return im.format, w, h
    except Exception:
        raise SSError("that file is not a readable image (use PNG or JPG)")


@mcp.tool()
@_guard
def replace_floorplan(survey_id: str, image_path: str = "", image_base64: str = "", allow_size_change: bool = False, apply: bool = False) -> dict:
    """Swap the background floor-plan image of one of the user's own surveys, keeping every element exactly where it is (positions are plan pixels, so a new image should have the same pixel size - otherwise elements land in the wrong place; a size change is refused unless allow_size_change=true). Give the image as image_path (a PNG/JPG inside the server data folder) or image_base64. The old image is saved first to the data folder (backups/floorplan-<id>-<time>.png/jpg); to undo, run replace_floorplan again with that file as image_path. Dry run unless apply=true."""
    d = C.survey(survey_id)
    if image_path:
        raw = _confine(image_path).read_bytes()
    elif image_base64:
        raw = base64.b64decode(image_base64.split(",")[-1], validate=False)
    else:
        return {"error": "give image_path (a file inside the data folder) or image_base64"}
    if len(raw) > 15_000_000:
        return {"error": "image is over 15 MB"}
    fmt, w, h = _img_info(raw)
    if fmt not in ("PNG", "JPEG"):
        return {"error": f"image must be PNG or JPG, got {fmt}"}
    if w > 12000 or h > 12000:
        return {"error": f"image is {w}x{h}; the limit is 12000 px per side"}
    old = C.floorplan_bytes(survey_id)
    ofmt, ow, oh = _img_info(old)
    info = {"survey": d.get("title"), "elements_kept": len(d.get("elements", [])), "current_plan_px": [ow, oh], "new_plan_px": [w, h], "new_kb": len(raw) // 1024}
    same = (ow, oh) == (w, h)
    if not same:
        info["warning"] = "the new image is a different pixel size; elements keep their pixel positions and will not line up with the new drawing unless allow_size_change=true is a deliberate choice"
    if not apply:
        return {"dry_run": True, **info}
    why = _gate(d)
    if why:
        return {"saved": False, "blocked": why}
    if not same and not allow_size_change:
        return {"saved": False, "blocked": "image size differs from the current plan; pass allow_size_change=true if that is intended", **info}
    with _wlock:
        live = C.survey(survey_id)
        if live.get("version") != d.get("version"):
            return {"saved": False, "error": "survey changed while working; re-read and redo"}
        bk = OUT / "backups"
        bk.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        keep = bk / f"floorplan-{survey_id}-{stamp}.{'png' if ofmt == 'PNG' else 'jpg'}"
        keep.write_bytes(old)
        C.upload_floorplan(survey_id, raw, "floorplan." + ("png" if fmt == "PNG" else "jpg"), "image/png" if fmt == "PNG" else "image/jpeg")
        got = _img_info(C.floorplan_bytes(survey_id))
    ok = (got[1], got[2]) == (w, h)
    _journal("replace_floorplan", survey_id, d.get("title"), old_px=[ow, oh], new_px=[w, h], verified=ok)
    return {"saved": True, **info, "old_image_saved_as": keep.name, "verified": {"ok": ok, "plan_px_now": [got[1], got[2]]},
            "undo": f"replace_floorplan(survey_id, image_path='{keep}', apply=true)"}


def _load_doc(ref):
    if ref.startswith("backup:"):
        p = OUT / "backups" / Path(ref[7:]).name
        if not p.is_file():
            raise SSError(f"no backup named {ref[7:]} (use list_backups)")
        return json.loads(p.read_text())
    return C.survey(ref)


def _keyed(d):
    out, seen = {}, collections.Counter()
    for e in d.get("elements", []):
        k = str(_a(e, 141) or e.get("name") or e["id"])
        seen[k] += 1
        out[k if seen[k] == 1 else f"{k}#{seen[k]}"] = e
    return out


@mcp.tool()
@_guard
def survey_diff(survey_a_id: str, survey_b_id: str, limit: int = 60) -> dict:
    """Compare two surveys (or a survey with a snapshot: pass 'backup:<file from list_backups>') and list what was added, removed and changed going from A to B, with before/after values for each attribute and moves in plan pixels. Elements are matched by their ID (FCAM-001), so it works between a survey and its copy. Use it for 'what changed between versions'. Read-only."""
    A, B = _load_doc(survey_a_id), _load_doc(survey_b_id)
    ka, kb = _keyed(A), _keyed(B)
    ida = {e["id"]: k for k, e in ka.items()}
    idb = {e["id"]: k for k, e in kb.items()}
    pal = _pal()

    def tname(e):
        return pal.get(e.get("element_id"), {}).get("name", e.get("element_id"))

    def conn(e, names):
        return {side: names.get(((e.get("connections") or {}).get(side) or {}).get("id")) for side in ("start", "end")} if e.get("connections") else None

    # pair by internal uuid first (same survey/snapshot), then by ID text (a copy or a rebuilt survey)
    pairs = {ka_: kb_ for ka_, kb_ in ((ida[u], idb[u]) for u in ida if u in idb)}
    left_a = [k for k in ka if k not in pairs]
    taken = set(pairs.values())
    for k in left_a:
        base = k.split("#")[0]
        if k in kb and k not in taken:
            pairs[k] = k
            taken.add(k)
        elif base in kb and base not in taken:
            pairs[k] = base
            taken.add(base)
    changed = []
    for k, k2 in pairs.items():
        x, y = ka[k], kb[k2]
        ax, ay = _norm_attrs(x), _norm_attrs(y)
        ch = {f"{i} {C.attr_name(i)}": {"before": ax.get(i), "after": ay.get(i)} for i in sorted(set(ax) | set(ay)) if ax.get(i, "") != ay.get(i, "")}
        row = {"id": k2 if k == k2 else f"{k} -> {k2}", "type": tname(y)}
        if ch:
            row["attributes"] = ch
        px, py = x.get("position"), y.get("position")
        if isinstance(px, dict) and isinstance(py, dict):
            if math.hypot(px["x"] - py["x"], px["y"] - py["y"]) > 0.5:
                row["moved"] = {"from": [round(px["x"], 1), round(px["y"], 1)], "to": [round(py["x"], 1), round(py["y"], 1)]}
        elif _pos(x) != _pos(y):
            row["route_changed"] = True
        if conn(x, ida) != conn(y, idb):
            row["connections"] = {"before": conn(x, ida), "after": conn(y, idb)}
        if len(row) > 2:
            changed.append(row)
    added = [{"id": k, "type": tname(e), "model": _a(e, 305)} for k, e in kb.items() if k not in taken]
    removed = [{"id": k, "type": tname(e), "model": _a(e, 305)} for k, e in ka.items() if k not in pairs]
    meta = {f: {"before": A.get(f), "after": B.get(f)} for f in ("title", "floorplan_scale", "unit", "icon_size") if A.get(f) != B.get(f)}
    return {"a": A.get("title"), "b": B.get("title"), "survey_fields_changed": meta,
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed), "unchanged": len(ka) - len(removed) - len(changed)},
            "added": added[:limit], "removed": removed[:limit], "changed": changed[:limit],
            "truncated": max(len(added), len(removed), len(changed)) > limit}


_RC = {"t": 0, "rows": [], "done": True}


def _since(v):
    v = str(v).strip()
    try:
        return float(v)
    except ValueError:
        try:
            dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise SSError("since_utc must look like 2026-09-30T12:00:00Z (or epoch seconds)")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()


@mcp.tool()
@_guard
def recent_changes(since_utc: str, site_id: str = "", favorites_only: bool = False, search: str = "", limit: int = 50) -> dict:
    """Change feed: which surveys were modified since a time (ISO like 2026-09-30T12:00:00Z, or epoch seconds), newest first, with site, title, element count and who has it claimed for editing; plus what was changed through THIS server (journal). Scans every site in the account unless narrowed with site_id / favorites_only / search, so for a cron watcher pass favorites_only or a search. Results are cached for 5 minutes. To see WHAT changed inside a survey, compare it with survey_diff (against a copy or a 'backup:' snapshot). Read-only."""
    t0 = _since(since_utc)
    key = (site_id, favorites_only, search)
    if _RC.get("key") != key or time.time() - _RC["t"] > 300:
        sites = [{"id": site_id, "name": ""}] if site_id else C.sites(search or None, favorites_only)
        if not C.tok.get("token"):
            C.refresh()
        rows, done = [], True
        deadline = time.time() + 100

        def one(st):
            if time.time() > deadline:
                return None
            return [{"site": st["name"], "site_id": st["id"], "survey_id": x["id"], "title": x.get("title"), "elements": x.get("element_count"),
                     "modified": x.get("modified_at"), "claimed_by": (x.get("editor") or {}).get("first_name") if isinstance(x.get("editor"), dict) else x.get("editor")}
                    for x in C.surveys(st["id"])]

        with concurrent.futures.ThreadPoolExecutor(6) as ex:
            for res in ex.map(one, sites):
                if res is None:
                    done = False
                else:
                    rows += res
        _RC.update(t=time.time(), rows=rows, done=done, key=key, sites=len(sites))
    hits = sorted((dict(r) for r in _RC["rows"] if (r["modified"] or 0) >= t0), key=lambda r: -(r["modified"] or 0))
    for r in hits:
        r["modified_utc"] = datetime.datetime.fromtimestamp(r["modified"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jr = []
    try:
        for ln in (OUT / "journal.jsonl").read_text().splitlines()[-400:]:
            j = json.loads(ln)
            tt = datetime.datetime.strptime(j["t"], "%Y%m%d-%H%M%S").timestamp()
            if tt >= t0:
                jr.append(j)
    except Exception:
        pass
    return {"since": datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sites_scanned": _RC.get("sites"),
            "scan_complete": _RC["done"], "surveys_modified": len(hits), "surveys": hits[:limit], "changed_through_this_server": jr[-limit:][::-1],
            "note": "journal entries use the server's local clock" if jr else ""}


# ---------------- cable paths ----------------
_SIDE = {"L": lambda w: (0, w / 2), "R": lambda w: (w, w / 2), "T": lambda w: (w / 2, 0), "B": lambda w: (w / 2, w)}


def _cable_type(v):
    opts = C.templates()[CABLE]["attrs"].get(526, {}).get("options") or []
    if not v:
        return None
    for o in opts:
        if o.lower() == v.strip().lower():
            return o
    raise SSError(f"cable_type '{v}' is not one of this team's options: {', '.join(opts)}")


@mcp.tool()
@_guard
def add_cable_path(survey_id: str, from_id: str, to_id: str, cable_type: str = "", length_ft: float = -1, apply: bool = False) -> dict:
    """Draw a cable run between two devices (uuid or name like WRCV-001), attached to their edges like the web app does, with cable type (one of the team's options, e.g. CAT6) and length. length_ft left out = measured from the drawing (distance x the plan scale; only for imperial plans). Cable paths feed cable_ft in bom/quote. Refuses a duplicate run between the same two devices. Dry run unless apply=true."""
    d = C.survey(survey_id)
    if from_id == to_id:
        return {"error": "from_id and to_id are the same device"}
    a, b = _resolve(d, [from_id, to_id])
    if a["id"] == b["id"]:
        return {"error": "from_id and to_id are the same device"}
    for e in (a, b):
        if e.get("element_id") == CABLE or not isinstance(e.get("position"), dict):
            return {"error": f"{e.get('name')} is not a device with a position (cable paths connect devices)"}
    for c in d.get("elements", []):
        if c.get("element_id") == CABLE:
            ends = {((c.get("connections") or {}).get(x) or {}).get("id") for x in ("start", "end")}
            if ends == {a["id"], b["id"]}:
                return {"error": f"{c.get('name')} already connects {a.get('name')} and {b.get('name')}; use set_cable_path to change it"}
    ctype = _cable_type(cable_type)
    w = float(d.get("icon_size") or 10) * 0.981
    pa, pb = a["position"], b["position"]
    dx, dy = pb["x"] - pa["x"], pb["y"] - pa["y"]
    sa, sb = (("R", "L") if dx >= 0 else ("L", "R")) if abs(dx) >= abs(dy) else (("B", "T") if dy >= 0 else ("T", "B"))
    p1 = {"x": round(pa["x"] + _SIDE[sa](w)[0], 2), "y": round(pa["y"] + _SIDE[sa](w)[1], 2)}
    p2 = {"x": round(pb["x"] + _SIDE[sb](w)[0], 2), "y": round(pb["y"] + _SIDE[sb](w)[1], 2)}
    if length_ft < 0:
        if d.get("unit") != "imperial" or not d.get("floorplan_scale"):
            return {"error": "cannot measure the length on this plan (not imperial or no scale); pass length_ft"}
        length_ft = round(math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"]) * float(d["floorplan_scale"]))
        measured = True
    else:
        if not math.isfinite(length_ft) or length_ft > 5000:
            return {"error": "length_ft must be between 0 and 5000"}
        measured = False
    e = C.new_element(d, CABLE, 0, 0, attributes={524: f"{length_ft:g}", **({526: ctype} if ctype else {})})
    e.update(variant="polyline", position=[p1, p2], connections={"start": {"id": a["id"], "attachment_point": sa}, "end": {"id": b["id"], "attachment_point": sb}})
    d["elements"].append(e)
    row = {"name": e["name"], "from": a.get("name"), "to": b.get("name"), "cable_type": ctype or "None", "length_ft": length_ft, "length_measured_from_plan": measured}
    if not apply:
        return {"dry_run": True, "would_add": row}
    return _commit(d, "add_cable_path", {"added": [row]})


@mcp.tool()
@_guard
def set_cable_path(survey_id: str, path_id: str, cable_type: str = "", length_ft: float = -1, apply: bool = False) -> dict:
    """Set the cable type (one of the team's options, e.g. CAT6) and/or length in feet on an existing cable path (uuid or name like CP-001). Leaves its drawn route and connections alone. Use survey_gaps to find paths missing a type/length. Dry run unless apply=true."""
    d = C.survey(survey_id)
    e = _resolve(d, [path_id])[0]
    if e.get("element_id") != CABLE:
        return {"error": f"{e.get('name')} is not a cable path"}
    ctype = _cable_type(cable_type)
    if not ctype and length_ft < 0:
        return {"error": "give cable_type and/or length_ft"}
    was = {"cable_type": _a(e, 526), "length_ft": _a(e, 524)}
    if ctype:
        C.set_attr(e, 526, ctype)
    if length_ft >= 0:
        if not math.isfinite(length_ft) or length_ft > 5000:
            return {"error": "length_ft must be between 0 and 5000"}
        C.set_attr(e, 524, f"{length_ft:g}")
    row = {"name": e.get("name"), "was": was, "now": {"cable_type": _a(e, 526), "length_ft": _a(e, 524)}}
    if not apply:
        return {"dry_run": True, **row}
    return _commit(d, "set_cable_path", {"count": 1, **row})


# ---------------- copy between surveys / price import ----------------
@mcp.tool()
@_guard
def copy_elements(source_survey_id: str, target_survey_id: str, ids: list[str], dx: float = 0, dy: float = 0, apply: bool = False) -> dict:
    """Copy elements from one survey into another of the user's own surveys (splitting/merging, e.g. Office vs Cameras). The source is only read. Copies get new internal ids; an ID that already exists in the target gets the next free number. Cable paths copy only if BOTH of their devices are in the copy (their connections are re-pointed to the copies). dx/dy shift the copies in plan pixels (default: same spot, so use it if the two plans are not the same drawing). Max 40 per save. Dry run unless apply=true."""
    if source_survey_id == target_survey_id:
        return {"error": "source and target are the same survey"}
    if not (math.isfinite(dx) and math.isfinite(dy)) or abs(dx) > 50000 or abs(dy) > 50000:
        return {"error": "dx/dy out of range"}
    src, tgt = C.survey(source_survey_id), C.survey(target_survey_id)
    if not ids:
        return {"error": "ids is empty"}
    picked = _resolve(src, ids)
    idset = {e["id"] for e in picked}
    for e in picked:
        if e.get("element_id") == CABLE:
            for side in ("start", "end"):
                if ((e.get("connections") or {}).get(side) or {}).get("id") not in idset:
                    return {"error": f"cable path {e.get('name')} connects to a device that is not in the list; add that device too, or leave the cable out"}
    left_behind = [c.get("name") for c in _cables_touching(src, idset)]
    new_ids = {e["id"]: str(uuid.uuid4()) for e in picked}
    used = {str(_a(e, 141) or e.get("name")) for e in tgt.get("elements", [])}
    rows, made = [], []
    for e in picked:
        c = copy.deepcopy(e)
        c["id"] = new_ids[e["id"]]
        for k in ("photos", "pdfs", "web_links", "children", "activity_log"):
            if k in c:
                c[k] = []
        old = str(_a(e, 141) or e.get("name"))
        nm = old if old not in used else C.next_id(tgt, e["element_id"], extra=used)
        used.add(nm)
        C.set_attr(c, 141, nm)
        c["name"] = nm
        pos = c.get("position")
        if isinstance(pos, dict):
            c["position"] = {"x": round(pos["x"] + dx, 2), "y": round(pos["y"] + dy, 2)}
        elif isinstance(pos, list):
            c["position"] = [{"x": round(p["x"] + dx, 2), "y": round(p["y"] + dy, 2)} for p in pos]
        if c.get("connections"):
            c["connections"] = {k: {**v, "id": new_ids[v["id"]]} for k, v in c["connections"].items()}
        made.append(c)
        rows.append({"from": old, "as": nm, "type": _pal().get(e["element_id"], {}).get("name", e["element_id"])})
    base_idx = max([x.get("element_index", 0) for x in tgt.get("elements", [])] + [0])
    base_z = max([x.get("z_order", 0) for x in tgt.get("elements", [])] + [0])
    for i, c in enumerate(made, 1):
        c["element_index"], c["z_order"] = base_idx + i, base_z + i
    tgt["elements"] += made
    info = {"copying": len(rows), "into": tgt.get("title"), "from": src.get("title"), "elements": rows}
    if left_behind:
        info["cable_paths_not_copied"] = f"these cables touch the copied devices in the source and were left out: {', '.join(map(str, left_behind[:12]))}"
    if not apply:
        return {"dry_run": True, **info}
    return _commit(tgt, "copy_elements", {"added": rows, **{k: v for k, v in info.items() if k != "elements"}})


def _money(v):
    try:
        x = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return x if math.isfinite(x) and 0 <= x <= 1_000_000 else None


@mcp.tool()
@_guard
def import_price_book(csv_text: str, survey_id: str, overwrite: bool = False, apply: bool = False) -> dict:
    """Fill the Device Price (attr 532) of ONE survey's elements from a pasted CSV (columns: a model/part column and a price column; header names auto-detected). Matches by model number. This only touches that survey - never the team's shared presets. Elements that already have a different price are left alone unless overwrite=true. Dry run shows matches, CSV rows that matched nothing, and elements with a model but no price in the CSV. IMPORTANT: ask the user whether the CSV prices are cost or list/sell - the quote tool treats the survey price as COST and adds the markup. Max 40 per save. Dry run unless apply=true."""
    d = C.survey(survey_id)
    if len(csv_text) > 500_000:
        return {"error": "CSV text is over 500 KB"}
    rows = list(csv.reader(io.StringIO(csv_text.strip())))
    if len(rows) < 2:
        return {"error": "need a header row and at least one data row"}
    head = [h.strip().lower() for h in rows[0]]
    mi = next((i for i, h in enumerate(head) if h in ("model", "part", "part number", "part #", "part no", "mpn", "sku", "product code", "item")), None)
    pi = next((i for i, h in enumerate(head) if h in ("price", "unit price", "cost", "unit cost", "list price", "sell price", "list", "msrp", "dealer price")), None)
    if mi is None or pi is None:
        return {"error": f"could not find a model column and a price column in the header {rows[0]}"}
    book, bad, dup = {}, [], []
    for r in rows[1:]:
        if len(r) <= max(mi, pi) or not r[mi].strip():
            continue
        p = _money(r[pi])
        k = Q.norm(r[mi])
        if p is None:
            bad.append(r[mi])
            continue
        if k in book and book[k][1] != p:
            dup.append(r[mi])
        book[k] = (r[mi].strip(), p)
    if dup:
        return {"error": "the CSV gives different prices for the same model: " + ", ".join(sorted(set(dup))[:10]) + ". Fix the CSV and retry."}
    hit, skipped, nomatch_el, used = [], [], [], set()
    for e in d.get("elements", []):
        if e.get("element_id") == CABLE:
            continue
        m = _a(e, 305)
        if not m:
            continue
        k = Q.norm(m)
        if k not in book:
            nomatch_el.append(e.get("name"))
            continue
        used.add(k)
        old = _a(e, 532)
        new = book[k][1]
        if old not in (None, "") and _money(old) == new:
            continue
        if old not in (None, "") and not overwrite:
            skipped.append({"name": e.get("name"), "model": m, "has": old, "csv": new})
            continue
        C.set_attr(e, 532, f"{new:g}")
        hit.append({"name": e.get("name"), "model": m, "was": old or None, "now": new})
    info = {"count": len(hit), "will_set": hit[:60], "left_alone_has_different_price": skipped[:30],
            "csv_rows_matching_nothing": [v[0] for k, v in book.items() if k not in used][:40], "elements_with_model_but_not_in_csv": nomatch_el[:40],
            "unreadable_prices": bad[:20]}
    if not hit:
        return {"note": "nothing to change", **info}
    if not apply:
        return {"dry_run": True, **info, "ask": "are these prices your cost or list/sell?"}
    return _commit(d, "import_price_book", info)


# ---------------- customer-facing draft PDF ----------------
@mcp.tool()
@_guard
def export_quote_pdf(survey_id: str, pricebook_path: str = "", labor_rate: float = DEF_LABOR, cable_per_ft: float = DEF_CABLE,
                     markup_pct: float = DEF_MARKUP, tax_pct: float = DEF_TAX, customer: str = "", allow_gaps: bool = False) -> dict:
    """Render the priced survey as a DRAFT quote PDF (equipment lines at sell price = cost + markup, cable, labor, total; no cost or markup shown) and return a download link. Refuses while any device has no model or price, unless allow_gaps=true (those lines then print as TBD). It is stamped DRAFT: the official customer document is the Salesforce CPQ quote (cpq_generate_document). Survey prices are treated as cost, like the quote tool. Read-only."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    d = C.survey(survey_id)
    pb = Q.load_pricebook(str(_confine(pricebook_path))) if pricebook_path else {}
    lr = labor_rate if labor_rate >= 0 else None
    cf = cable_per_ft if cable_per_ft >= 0 else None
    q = Q.build_quote(d, _pal(), C.profiles(), pb, lr, cf, markup_pct, tax_pct)
    if q["gaps"] and not allow_gaps:
        return {"error": "the quote has gaps, so no customer PDF was made (fix them, or pass allow_gaps=true for a draft with TBD lines)", "gaps": q["gaps"][:40], "gap_count": len(q["gaps"])}
    st = getSampleStyleSheet()
    money = lambda v: "TBD" if v is None else f"${v:,.2f}"
    body, total = [], 0.0
    for l in q["lines"]:
        unit = None if l["unit_price"] is None else round(l["unit_price"] * (1 + markup_pct / 100.0), 2)
        ext = None if unit is None else round(unit * l["qty"], 2)
        total += ext or 0
        body.append([Paragraph(f'{l["type"]}<br/><font size=8 color="#555555">{(l["manufacturer"] + " " + l["model"]).strip() or "model not set"}</font>', st["BodyText"]), l["qty"], money(unit), money(ext)])
    if q["cable_ft"]:
        c = None if cf is None else round(q["cable_ft"] * cf, 2)
        total += c or 0
        body.append(["Cable and installation materials", f'{q["cable_ft"]:g} ft', money(cf), money(c)])
    if q["labor_hours"]:
        lab = None if lr is None else round(q["labor_hours"] * lr, 2)
        total += lab or 0
        body.append(["Installation labor", f'{q["labor_hours"]:g} hrs', money(lr), money(lab)])
    tax = round(total * tax_pct / 100.0, 2)
    rows = [["Item", "Qty", "Unit", "Amount"]] + body
    if tax_pct:
        rows += [["", "", "Subtotal", money(round(total, 2))], ["", "", f"Tax {tax_pct:g}%", money(tax)]]
    rows.append(["", "", "TOTAL", money(round(total + tax, 2))])
    od = OUT / "quotes"
    od.mkdir(parents=True, exist_ok=True)
    p = od / (re.sub(r"[^\w\-]+", "_", d.get("title") or survey_id) + "_DRAFT_quote.pdf")

    def stamp(cv, doc):
        cv.saveState()
        cv.setFont("Helvetica-Bold", 90)
        cv.setFillColor(colors.Color(0.85, 0.85, 0.85, alpha=0.35))
        cv.translate(letter[0] / 2, letter[1] / 2)
        cv.rotate(40)
        cv.drawCentredString(0, 0, "DRAFT")
        cv.restoreState()
        cv.setFont("Helvetica", 8)
        cv.setFillColor(colors.grey)
        cv.drawString(0.75 * inch, 0.5 * inch, "DRAFT generated from a site survey for internal review. Official pricing is the Salesforce CPQ quote.")

    doc = SimpleDocTemplate(str(p), pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.75 * inch, bottomMargin=0.8 * inch, title=f"DRAFT quote - {d.get('title')}")
    t = Table(rows, colWidths=[3.8 * inch, 0.8 * inch, 1.1 * inch, 1.3 * inch], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f3f3f3")]),
                           ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black)]))
    doc.build([Paragraph("DRAFT Quote", st["Title"]), Paragraph(f'{d.get("title") or ""}', st["Heading3"]),
               Paragraph(f'{("Prepared for " + customer + " - ") if customer else ""}{time.strftime("%B %d, %Y")}', st["Normal"]), Spacer(1, 14), t], onFirstPage=stamp, onLaterPages=stamp)
    return {"pdf": str(p), "link": _link(p), "total": round(total + tax, 2), "lines": len(body), "gaps_shown_as_TBD": len(q["gaps"]),
            "note": "DRAFT only. Build the real customer quote in Salesforce CPQ (cpq_add_lines, then cpq_generate_document)."}


# ---------------- misc ----------------
@mcp.tool()
@_guard
def download_floorplan(survey_id: str, path: str = "") -> dict:
    """Save the raw floor-plan image to disk and return the path."""
    d = C.survey(survey_id)
    url = d.get("floorplan_url")
    if not url:
        return {"error": "survey has no floor plan image"}
    key = url.split("media/")[1] if "media/" in url else None
    if not key:
        return {"error": "floor plan URL is in an unexpected form"}
    r = httpx.get(C.get("/storage/media/presign", params={"key": key})["url"], follow_redirects=True, timeout=60)
    r.raise_for_status()
    ext = "png" if "png" in r.headers.get("content-type", "") else "jpg"
    p = _confine(path) if path else OUT / f"floorplan_{survey_id}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    return {"path": str(p), "bytes": len(r.content), "scale": d.get("floorplan_scale")}


@mcp.tool()
@_guard
def raw_get(path: str) -> dict:
    """Read-only GET against /v3 (e.g. /user). For exploring the API."""
    path = path if path.startswith("/") else "/" + path
    if ".." in path or "//" in path or "@" in path:
        return {"error": "not a valid API path"}
    return C.get(path)


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
    df = _diff(live, snap)
    summary = {"would_remove": [_nm(live, i) for i in df["removed"]], "would_re_add": [_nm(snap, i) for i in df["added"]],
               "would_revert_changes_on": [_nm(live, i) for i in df["changed"]]}
    if _meta(live, snap):
        summary["would_revert_title"] = {"from": live.get("title"), "to": snap.get("title")}
    if not apply:
        return {"dry_run": True, **summary}
    snap["version"] = live.get("version")
    return _commit(snap, "restore_backup", summary, force_count=len(df["changed"]) + len(df["removed"]) + len(df["added"]) + len(_meta(live, snap)))


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
    if not exp.isdigit() or p is None or p.suffix.lower() not in (".png", ".jpg", ".xlsx", ".csv", ".pdf") or not hmac.compare_digest(sig, want) or int(exp) < time.time():
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
