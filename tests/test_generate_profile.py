"""Offline tests for the profile card generator (no network, fixed data)."""

import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Self

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import generate_profile as gp

TODAY = date(2026, 10, 10)
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture
def stats() -> gp.Stats:
    return gp.Stats(
        created=date(2019, 8, 1),
        last_year=15_747,
        all_time=27_197,
        commits=10_632,
        prs=477,
        issues=119,
        files=4_321,
        loc=1_234_567,
        repos=69,
        days=tuple([0] * 300 + [3] * 64 + [0]),  # 64-day streak; today still at zero
        languages=(("Python", "#3572A5", 47.4), ("Rust", "#dea584", 23.1), ("C", "#555555", 29.5)),
    )


@pytest.fixture
def cards(stats: gp.Stats) -> dict[str, str]:
    return gp.render_all(stats, TODAY)


@pytest.mark.parametrize(("n", "expected"), [(0, "0"), (999, "999"), (1000, "1k"), (10_632, "10.6k"), (27_197, "27.2k"), (1_000_000, "1M"), (1_234_567, "1.2M")])
def test_fmt_k(n: int, expected: str) -> None:
    assert gp.fmt_k(n) == expected


def test_streaks_ignore_a_quiet_today() -> None:
    assert gp.streaks((1, 1, 0, 2, 2, 2, 0)) == (3, 3)
    assert gp.streaks((0, 0)) == (0, 0)


def test_uptime() -> None:
    assert gp.uptime(date(2019, 8, 1), TODAY) == "7y 2m"


def test_aggregate_languages_excludes_markup_and_folds_the_tail() -> None:
    def repo(*langs: tuple[str, int]) -> dict:
        return {"languages": {"edges": [{"size": s, "node": {"name": n, "color": None}} for n, s in langs]}}

    out = gp.aggregate_languages([repo(("Python", 600), ("HTML", 9_000)), repo(("Rust", 300), ("Go", 100))], top=2)
    names = [n for n, _, _ in out]
    assert names == ["Python", "Rust", "Other"]
    assert "HTML" not in names
    assert sum(p for _, _, p in out) == pytest.approx(100)


def test_every_card_is_well_formed_svg_with_an_accessible_title(cards: dict[str, str]) -> None:
    assert set(cards) == {"header.svg", "stats.svg", "languages.svg"}
    for svg in cards.values():
        root = ET.fromstring(svg)
        assert root.tag == f"{SVG_NS}svg"
        assert root.get("role") == "img"
        title = root.find(f"{SVG_NS}title")
        assert title is not None and title.text


def test_cards_are_safe_for_github_camo(cards: dict[str, str]) -> None:
    for svg in cards.values():
        lowered = svg.lower()
        assert "<script" not in lowered
        assert "href=\"http" not in lowered and "url(http" not in lowered  # no external fetches


def test_text_meets_wcag_aa() -> None:
    assert gp.text_audit(gp.THEME) >= gp.AA


def test_language_marks_meet_non_text_contrast() -> None:
    surfaces = gp.surfaces(gp.THEME)
    fill = gp.lang_fill(gp.THEME, "#555555").split('"')[1]
    assert min(gp.contrast(fill, s) for s in surfaces) >= gp.AA_LARGE


def test_stats_row_shows_real_commits_not_stars(stats: gp.Stats) -> None:
    row = dict(gp.stat_row(stats, TODAY))
    assert row["COMMITS"] == "10.6k"
    assert "STARS" not in row
    assert list(row) == ["STREAK", "PRS", "ISSUES", "REPOS", "COMMITS", "UPTIME"]


def test_stats_card_shows_code_totals(cards: dict[str, str]) -> None:
    svg = cards["stats.svg"]
    assert ">1.2M</text>" in svg and ">4,321</text>" in svg
    assert "LINES OF CODE" in svg and ">FILES<" in svg and "TOTAL CODE" in svg
    assert "LAST 12 MONTHS" not in svg  # captions must describe what the numbers are


def test_sum_scc_counts_only_source() -> None:
    rows = [{"Name": "Python", "Count": 10, "Code": 900}, {"Name": "Rust", "Count": 5, "Code": 400},
            {"Name": "JSON", "Count": 50, "Code": 99_999}, {"Name": "Markdown", "Count": 7, "Code": 300},
            {"Name": "HTML", "Count": 3, "Code": 5_000}]
    assert gp.sum_scc(rows) == (15, 1_300)


def test_header_has_subtitle_and_no_kicker(cards: dict[str, str]) -> None:
    svg = cards["header.svg"]
    assert gp.TAGLINE in svg
    assert "MARKETS" not in svg


def test_each_card_embeds_only_the_fonts_it_uses(cards: dict[str, str]) -> None:
    assert "font-family:'Chakra Petch';font-weight:700" not in cards["header.svg"]
    assert "font-family:'Audiowide'" not in cards["languages.svg"]
    assert "font-family:'Chakra Petch'" not in cards["stats.svg"]


def test_typing_glyphs_exist_in_vt323(stats: gp.Stats) -> None:
    # The latin VT323 subset has no → or ❯; those would silently fall back to a system font.
    assert not any(ch in line for line in gp.typing_lines(stats) for ch in "→❯")


def test_private_repo_names_never_leave_fetch(cards: dict[str, str]) -> None:
    # Only aggregate language names are rendered; the fixture has no repo names, and no card should invent any.
    for svg in cards.values():
        assert "nameWithOwner" not in svg


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _http_error(code: int) -> Exception:
    import urllib.error

    return urllib.error.HTTPError(gp.API, code, "boom", {}, None)  # type: ignore[arg-type]


def test_graphql_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: int) -> _Resp:
        calls.append(1)
        if len(calls) < 3:
            raise _http_error(502)
        return _Resp(b'{"data": {"user": {"ok": true}}}')

    monkeypatch.setattr(gp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)
    assert gp.graphql("t", "q") == {"ok": True}
    assert len(calls) == 3


def test_graphql_fails_fast_on_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: int) -> _Resp:
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(gp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)
    with pytest.raises(Exception, match="boom"):
        gp.graphql("t", "q")
    assert len(calls) == 1


def test_clone_failures_never_echo_secrets_or_names(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                     tmp_path: Path) -> None:
    import subprocess

    def boom(cmd: list[str], **kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd + ["SECRET-TOKEN", "private-repo-name"], 300)

    monkeypatch.setattr(gp.subprocess, "run", boom)
    assert gp._checkout_small_files("https://github.com/o/private-repo-name.git", tmp_path / "0", {}) is False
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err and "private-repo-name" not in out.out + out.err
