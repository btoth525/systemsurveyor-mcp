"""Offline tests: no network, no System Surveyor login needed. Run: pip install pytest && pytest"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(DATA_DIR=tempfile.mkdtemp(), OWNER_USER_ID="111", SS_TEAM_ID="7", SS_ACCOUNT_ID="1", WRITE_SURVEYS="allowlisted-id")

import pytest
import quote as Q
import server as S
from ssapi import SSError


def el(name, element_id, **attrs):
    return {"id": name, "name": name, "element_id": element_id,
            "attributes": [{"attribute_id": int(k), "name": str(k), "value": str(v)} for k, v in attrs.items()]}


def test_norm_ignores_punctuation_and_case():
    assert Q.norm("cd53-e ") == Q.norm("CD53E")


def test_quote_math():
    doc = {"title": "t", "elements": [
        el("c1", 69, **{"305": "CD53-E", "271": "Acme", "532": "100", "531": "2", "533": "1.5"}),
        el("p1", 65, **{"524": "100", "521": "20", "526": "CAT6"}),
    ]}
    q = Q.build_quote(doc, {}, [], labor_rate=100.0, cable_per_ft=0.5, markup_pct=50.0)
    assert q["equipment"] == 200.0 and q["markup"] == 100.0       # price x qty, then markup
    assert q["cable_ft"] == 120.0 and q["cable"] == 60.0           # length + additional
    assert q["labor_hours"] == 3.0 and q["labor"] == 300.0         # hours x qty
    assert q["total"] == 660.0 and q["gaps"] == []


def test_quote_reports_gaps_instead_of_guessing():
    q = Q.build_quote({"title": "t", "elements": [el("c1", 69)]}, {}, [])
    assert any("no model number" in g for g in q["gaps"])


def test_color_parsing():
    assert S._hex("#1a6bd7") == "1a6bd7" and S._hex("Purple") == "9B59B6"
    with pytest.raises(SSError):
        S._hex("banana")


def test_select_requires_a_scope():
    with pytest.raises(SSError):
        S._select({"elements": [el("a", 69)]}, [], 0, "")


def test_select_by_model_and_prefix():
    d = {"elements": [el("FCAM-001", 69, **{"305": "CD53-E"}), el("FCAM-002", 69), el("NSW-001", 66)]}
    assert [e["id"] for e in S._select(d, [], 0, "", model="cd53e")] == ["FCAM-001"]
    assert len(S._select(d, [], 0, "FCAM")) == 2


@pytest.mark.parametrize("survey,ok", [
    ({"id": "x", "team_id": 7, "creator": 111}, True),            # mine
    ({"id": "x", "team_id": 7, "creator": 222}, False),           # a coworker's
    ({"id": "x", "team_id": 8, "creator": 111}, False),           # another team
    ({"id": "allowlisted-id", "team_id": 7, "creator": 222}, True),
])
def test_write_permission(survey, ok):
    assert (S._may_write(survey) is None) is ok


def test_lens_colors_roundtrip():
    e = el("m", 257, **{"633": '[{"color":"F8C309","angle":90},{"color":"E6003E","angle":90}]'})
    assert [l["color"] for l in S._lenses(e)] == ["F8C309", "E6003E"]
    assert S._lenses(el("x", 69)) is None


def test_diff_counts_real_changes():
    a = {"elements": [dict(el("a", 69, **{"305": "X"}), position={"x": 1, "y": 2}), el("b", 69), el("c", 69)]}
    b = {"elements": [dict(el("a", 69, **{"305": "X"}), position={"x": 5, "y": 2}), el("b", 69, **{"305": "Y"}), el("d", 69)]}
    df = S._diff(a, b)
    assert sorted(df["changed"]) == ["a", "b"] and df["added"] == ["d"] and df["removed"] == ["c"]
    assert S._diff(a, a) == {"changed": [], "added": [], "removed": []}


def test_resolve_rejects_unknown_and_duplicates():
    d = {"elements": [el("u1", 69, **{"141": "FCAM-001"}), el("u2", 69, **{"141": "FCAM-002"}), el("u3", 69, **{"141": "FCAM-002"})]}
    assert S._resolve(d, ["FCAM-001"])[0]["id"] == "u1"
    with pytest.raises(SSError):
        S._resolve(d, ["FCAM-999"])
    with pytest.raises(SSError):
        S._resolve(d, ["FCAM-002"])


def test_xy_validation():
    assert S._xy({"x": "10", "y": 20.126}) == (10.0, 20.13)
    for bad in ({"x": 1}, {"x": "a", "y": 1}, {"x": -1, "y": 1}, {"x": float("nan"), "y": 1}, {"x": 1, "y": 99999}):
        with pytest.raises(SSError):
            S._xy(bad)


def test_cables_touching_finds_attached_paths():
    cp = dict(el("cp", 65), connections={"start": {"id": "dev"}, "end": {"id": "other"}})
    d = {"elements": [el("dev", 69), el("other", 69), cp, dict(el("cp2", 65), connections={})]}
    assert [c["id"] for c in S._cables_touching(d, {"dev"})] == ["cp"]
    assert S._cables_touching(d, {"dev", "cp"}) == []


def test_guard_turns_bad_input_into_error_dict():
    @S._guard
    def boom(m):
        return m["x"]
    out = boom({})
    assert "error" in out and "Nothing was changed" in out["error"]


def test_confine_only_in_server_mode(monkeypatch):
    monkeypatch.setattr(S, "HTTP_MODE", True)
    with pytest.raises(SSError):
        S._confine("/etc/passwd")
    with pytest.raises(SSError):
        S._confine("../../etc/passwd")
    assert str(S._confine("x.csv")).startswith(str(S.OUT.resolve()))
    monkeypatch.setattr(S, "HTTP_MODE", False)
    assert str(S._confine("/etc/passwd")).endswith("passwd")
