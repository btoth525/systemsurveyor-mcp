"""System Surveyor API client (reverse-engineered from the web app bundle)."""
import json, os, re, threading, time, uuid
from pathlib import Path
import httpx

BASE = "https://openapi2.systemsurveyor.com/v3"
WBASE = "https://openapi.systemsurveyor.com/v3"
RBASE = "https://resources.openapi.systemsurveyor.com"
TOKEN_FILE = Path(os.environ.get("SS_TOKEN_FILE", Path.home() / ".systemsurveyor" / "tokens.json"))

DROP_KEYS = {"photo_urls", "pdf_urls", "attributeHash", "sections", "group", "icon", "floorplan_url"}


class SSError(Exception):
    pass


class SSAuthError(SSError):
    """The refresh token was rejected: a human has to log in again."""


def _load():
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return {}


def _save(d):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(d))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


class Client:
    def __init__(self):
        self.http = httpx.Client(timeout=60)
        self.tok = _load()
        self._palette = None
        self._ctx = None
        self._lock = threading.RLock()
        self._stamp = {}

    # ---- auth ----
    def login(self, email, password):
        r = self.http.post(f"{BASE}/auth", json={"email": email, "password": password, "force_login": False})
        if r.status_code != 200:
            raise SSError(f"login failed {r.status_code}: {r.text[:300]}")
        self._store(r.json())

    def set_refresh_token(self, rt):
        self.tok = {"refreshToken": rt}
        self.refresh()

    def _store(self, j):
        d = j.get("data", j)
        self.tok = {
            "token": d.get("token"),
            "refreshToken": d.get("refresh_token") or d.get("refreshToken") or self.tok.get("refreshToken"),
        }
        _save(self.tok)

    def refresh(self):
        with self._lock:
            self._refresh()

    def _refresh(self):
        disk = _load()
        if disk.get("refreshToken"):
            self.tok["refreshToken"] = disk["refreshToken"]
        rt = self.tok.get("refreshToken")
        if not rt:
            raise SSError("Not logged in. Run login.py in the project folder.")
        try:
            r = self.http.post(f"{BASE}/auth/refresh_token", json={"refresh_token": rt})
        except httpx.HTTPError as e:
            raise SSError(f"refresh could not reach System Surveyor ({type(e).__name__}); will retry")
        if r.status_code in (400, 401, 403):
            raise SSAuthError(f"refresh rejected {r.status_code}: System Surveyor login expired, run login.py")
        if r.status_code != 200:
            raise SSError(f"refresh failed {r.status_code}; will retry")
        self._store(r.json())

    def req(self, method, path, base=None, **kw):
        if not self.tok.get("token"):
            self.refresh()
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self.tok['token']}", **(kw.get("headers") or {})}
            r = self.http.request(method, (base or BASE) + path, **{**kw, "headers": h})
            if r.status_code == 401 and attempt == 0:
                self.refresh()
                continue
            break
        if r.status_code >= 400:
            raise SSError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r

    def get(self, path, **kw):
        return self.req("GET", path, **kw).json()

    # ---- context ----
    def ctx(self):
        if not self._ctx:
            u = self.get("/user")
            self._ctx = u.get("data", u)
        return self._ctx

    @staticmethod
    def _need(name):
        v = os.environ.get(name)
        if not v:
            raise SSError(f"{name} is not set. Find it in the System Surveyor web app (see README) and put it in .env.")
        return int(v)

    def account_id(self):
        return self._need("SS_ACCOUNT_ID")

    def team_id(self):
        return self._need("SS_TEAM_ID")

    # ---- palette / templates ----
    def templates(self):
        """element_id -> {name, abbreviation, systemtype_id, color, attrs{id:{name,default}}} from /survey-templates."""
        if self._stale("_tpl"):
            raw = self.get("/survey-templates")
            out, names = {}, {}
            for t in raw:
                c = t["content"] if isinstance(t["content"], dict) else json.loads(t["content"])
                attrs = {}
                for sec in c.get("sections", []):
                    for a in sec.get("attributes", []) or []:
                        attrs[a["attribute_id"]] = {"name": a["name"], "default": a.get("default_value") or "",
                                                    "options": [v["value"] for v in (a.get("values") or []) if isinstance(v, dict) and "value" in v]}
                        names[a["attribute_id"]] = a["name"]
                st = c.get("systemType") or {}
                out[c["element_id"]] = {
                    "element_id": c["element_id"], "name": c["name"], "abbreviation": c.get("abbreviation", "ELEM-"),
                    "systemtype_id": st.get("systemtype_id"), "system": st.get("name"), "color": st.get("color"), "attrs": attrs,
                    "icon": c.get("icon") or "", "category": (c.get("category") or {}).get("elementcategory_id"),
                }
            self._tpl = out
            self._attr_names = names
        return self._tpl

    def _stale(self, name, ttl=3600):
        v = getattr(self, name, None)
        if not v or time.time() - self._stamp.get(name, 0) > ttl:
            self._stamp[name] = time.time()
            return True
        return False

    def attr_name(self, attr_id):
        self.templates()
        return self._attr_names.get(attr_id, str(attr_id))

    def palette(self):
        if self._stale("_palette"):
            t = self.team_id()
            groups = self.get(f"/team/{t}/elements")
            tpl = self.templates()
            by_id = {}
            for key, items in groups.items():
                for it in items:
                    x = tpl.get(it["element_id"], {})
                    by_id.setdefault(it["element_id"], {
                        "element_id": it["element_id"], "name": it["name"], "system": key,
                        "systemtype_id": x.get("systemtype_id"), "abbreviation": x.get("abbreviation"),
                    })
            self._palette = {"by_id": by_id}
        return self._palette

    def profiles(self):
        if self._stale("_profiles"):
            self._profiles = self.get(f"/team/{self.team_id()}/element_profiles")["element_profiles"]
        return self._profiles

    # ---- sites / surveys ----
    def sites(self, search=None, favorites=False, page_size=100):
        out, page = [], 1
        while True:
            q = {"page[number]": page, "page[size]": page_size, "sort": "name"}
            if favorites:
                q["filter[favorites]"] = "true"
            j = self.get(f"/accounts/{self.account_id()}/sites", params=q)
            out += j["data"].get("sites", [])
            if page >= j.get("meta", {}).get("total_pages", 1):
                break
            page += 1
        if search:
            s = search.lower()
            out = [x for x in out if s in x["name"].lower()]
        return out

    def surveys(self, site_id):
        j = self.get(f"/sites/{site_id}/surveys")
        return j.get("data", j)

    def survey(self, survey_id):
        j = self.get(f"/survey/{survey_id}")
        return j if "elements" in j else j.get("data", j)

    # ---- survey copy / floor plan ----
    def copy_survey(self, survey_id):
        """Server-side duplicate into the same site. Returns the job id (poll copy_status)."""
        j = self.req("POST", f"/survey/{survey_id}/copy").json()
        return j.get("job_id") or (j.get("data") or {}).get("job_id")

    def copy_status(self, survey_id, job):
        return self.req("GET", f"/survey/{survey_id}/copy/{job}/status").json()

    def floorplan_bytes(self, survey_id):
        return self.req("GET", f"/survey/{survey_id}/floorplan", base=RBASE).content

    def upload_floorplan(self, survey_id, data, name, ctype):
        r = self.req("POST", f"/survey/{survey_id}/floorplan", base=RBASE, files={"floorplan": (name, data, ctype)})
        return r.json() if r.content else {}

    # ---- native reports + exports (rendered by System Surveyor itself) ----
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def reports(self, site_id):
        j = self.get(f"/site/{site_id}/scheduled_reports")
        return j.get("data", j) if isinstance(j, dict) else j

    def report_download(self, file_path):
        """Fetch a finished native report's bytes (S3 presigned link; no auth header is sent to S3)."""
        u = self.get("/storage/reports/presign", params={"key": file_path})["url"]
        r = httpx.get(u, follow_redirects=True, timeout=180)
        r.raise_for_status()
        return r.content

    def create_report(self, site_id, name, survey_ids, report_types, config):
        return self.req("POST", f"/site/{site_id}/scheduled_reports", json={"name": name, "survey_ids": survey_ids,
                                                                           "report_types": report_types, "config": config}).json()

    def export_xlsx(self, survey_id, tries=150):
        """Native Excel export: start a job, poll until the workbook is ready. Returns the xlsx bytes."""
        job = self.req("POST", f"/survey/{survey_id}/export", base=WBASE).json().get("job_id")
        if not job:
            raise SSError("export did not return a job id")
        for _ in range(tries):
            time.sleep(2)
            try:
                r = self.req("GET", f"/survey/export/{job}", base=WBASE, headers={"Accept": self.XLSX})
            except SSError:
                continue
            if r.status_code == 200:
                return r.content
        raise SSError("export timed out")

    # ---- write path ----
    @staticmethod
    def _sval(v):
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, dict)):
            return json.dumps(v)
        return str(v)

    def _wire_element(self, e):
        e = {k: v for k, v in e.items() if k not in DROP_KEYS}
        e["attributes"] = [
            {"attribute_id": a["attribute_id"], "name": a.get("originalName") or a.get("name"), "value": self._sval(a.get("value"))}
            for a in e.get("attributes", [])
        ]
        names = {a["attribute_id"]: a["value"] for a in e["attributes"]}
        e["name"] = names.get(141) or names.get(140) or e.get("name")
        return e

    def save_survey(self, doc, site_id=None, keep_lock=False):
        sid = doc["id"]
        if site_id is None:
            s = doc.get("site")
            site_id = s["id"] if isinstance(s, dict) else s
        body = {k: v for k, v in doc.items() if k not in DROP_KEYS}
        body["elements"] = [self._wire_element(e) for e in doc.get("elements", [])]
        body["modified_source"] = "web"
        self.req("POST", f"/survey/{sid}/lock", base=WBASE)
        try:
            j = self.req("POST", f"/site/{site_id}/survey/{sid}/sync", base=WBASE, json=body).json()
            job = j.get("job_id") or j.get("data", {}).get("job_id")
            delay = 1.0
            for _ in range(60):
                r = self.req("GET", f"/survey/sync/{job}", base=WBASE)
                if r.status_code == 202:
                    time.sleep(delay)
                    delay += 0.5
                    continue
                res = r.json()
                if res.get("status") == "errored":
                    raise SSError(f"sync errored: {res}")
                return res
            raise SSError("sync timed out")
        finally:
            if not keep_lock:
                try:
                    self.req("DELETE", f"/survey/{sid}/lock", base=WBASE)
                except Exception:
                    pass

    def next_id(self, doc, element_id, extra=()):
        abbr = self.templates()[int(element_id)]["abbreviation"]
        rx = re.compile("^" + re.escape(abbr) + r"(\d+)$")
        used = [e.get("name") for e in doc.get("elements", [])] + list(extra)
        for e in doc.get("elements", []):
            for a in e.get("attributes", []):
                if a["attribute_id"] == 141:
                    used.append(a["value"])
        n = max([int(m.group(1)) for u in used if u and (m := rx.match(str(u)))] + [0]) + 1
        return f"{abbr}{n:03d}"

    def new_element(self, doc, element_id, x, y, name=None, attributes=None, profile=None):
        element_id = int(element_id)
        tpl = self.templates().get(element_id)
        if not tpl:
            raise SSError(f"unknown element_id {element_id}; use list_palette")
        els = doc.get("elements", [])
        idx = max([e.get("element_index", 0) for e in els] + [0]) + 1
        z = max([e.get("z_order", 0) for e in els] + [0]) + 1
        eid = name or self.next_id(doc, element_id)
        attrs = {141: eid, 140: tpl["name"], 138: "Proposed", 530: tpl["color"] or "", 531: "1"}
        for k, v in tpl["attrs"].items():
            if v["default"] != "" and k not in attrs:
                attrs[k] = v["default"]
        if profile:
            for a in profile["content"]["attribute"]:
                if a["value"] not in ("", None):
                    attrs[a["attribute_id"]] = a["value"]
        for k, v in (attributes or {}).items():
            attrs[int(k)] = v
        attrs[141] = eid
        alist = [{"attribute_id": k, "name": self.attr_name(k), "value": self._sval(v)} for k, v in attrs.items() if v not in (None, "")]
        return {
            "id": str(uuid.uuid4()), "name": eid, "element_id": element_id, "systemtype_id": tpl["systemtype_id"],
            "attributes": alist, "accessories": [], "activity_log": [], "children": [], "connections": {},
            "element_index": idx, "element_profile_id": profile["id"] if profile else 0,
            "pdfs": [], "photos": [], "web_links": [], "variant": "right_angle",
            "position": {"x": float(x), "y": float(y)}, "z_order": z,
        }

    @staticmethod
    def get_attr(el, attr_id):
        for a in el.get("attributes", []):
            if a["attribute_id"] == attr_id:
                return a.get("value")
        return None

    def set_attr(self, el, attr_id, value):
        for a in el["attributes"]:
            if a["attribute_id"] == attr_id:
                a["value"] = self._sval(value)
                return
        el["attributes"].append({"attribute_id": attr_id, "name": self.attr_name(attr_id), "value": self._sval(value)})
