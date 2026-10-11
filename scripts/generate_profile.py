"""Render animated profile SVG cards from live GitHub GraphQL stats.

Usage: GH_TOKEN=<token> python3 scripts/generate_profile.py [out_dir]

The token must belong to the profile owner so private contribution counts
are included. Private repository names are never written to any output.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

LOGIN = "Corfucinas"
API = "https://api.github.com/graphql"
EXCLUDED_LANGUAGES = frozenset({"HTML", "CSS", "SCSS"})  # generated site output would swamp real code
TOP_LANGUAGES = 7
WIDTH = 840
MARGIN, RIGHT = 40, 800  # shared left/right edges for every card
SAFE_X = 480  # header scene fades in right of this so nothing crosses text
KICKER_Y = 44  # shared baseline for every card's label
FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"  # OFL woff2 files embedded into SVGs
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
AA, AA_LARGE = 4.5, 3.0  # WCAG 2.x contrast minimums: body text, large text / meaningful graphics
GLOW = ' filter="url(#glow)"'
RETRIES = 3  # transient network/API failures; a failed run leaves yesterday's cards up
DISPLAY_ADV = {"0": 0.925, "1": 0.354, "2": 0.752, "3": 0.719, "4": 0.773, "5": 0.75, "6": 0.744, "7": 0.641,
               "8": 0.746, "9": 0.744, ",": 0.225, ".": 0.205, "k": 0.691, "M": 0.962}  # Audiowide advances (em), measured with fonttools
# Not "code" for the LOC/FILES count: markup, data, docs and config formats as scc names them.
NON_CODE = EXCLUDED_LANGUAGES | frozenset({
    "JSON", "JSONL", "YAML", "TOML", "XML", "SVG", "CSV", "TSV", "Markdown", "ReStructuredText", "Plain Text",
    "Text", "License", "Jupyter", "Org", "AsciiDoc", "LaTeX", "TeX", "INI", "Properties File", "gitignore",
})
MAX_FILE = 512_000  # bytes; larger files are data, binaries or generated output, not hand-written code
SCC_EXCLUDE_DIRS = "vendor,node_modules,dist,build,.venv,venv,third_party,target,site-packages"
TAGLINE = "I build, launch, and grow new ventures."


# --- colour & contrast -------------------------------------------------------

def luminance(hex_color: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(color: str, surfaces: list[str], ratio: float) -> str:
    """Nudge a colour toward white (dark UI) or black (light UI) until it meets
    `ratio` against every surface; keeps the hue as far as possible."""
    target = 255 if sum(map(luminance, surfaces)) / len(surfaces) < 0.4 else 0
    rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    mixed = color
    for step in range(21):
        mixed = "#" + "".join(f"{round(c + (target - c) * step / 20):02x}" for c in rgb)
        if min(contrast(mixed, s) for s in surfaces) >= ratio:
            return mixed
    return mixed


def surfaces(t: dict) -> list[str]:
    return [t["bg"], t["bg2"], t["panel"]]


def theme(**kw: Any) -> dict[str, Any]:
    """Palette plus text-only tokens derived to pass WCAG AA on every surface. Decorative colours are untouched."""
    t = dict(kw)
    for key, src in (("fg", "fg"), ("muted", "muted"), ("ta1", "a1"), ("ta2", "a2")):
        t[key] = readable(t[src], surfaces(t), AA)
    t["gstops"] = tuple(readable(c, surfaces(t), AA_LARGE) for c in (t["g1"], t["g2"]))
    return t


def text_audit(t: dict) -> float:
    """Lowest contrast of any text token against any surface it can sit on (large text scaled to the AA bar)."""
    small = min(contrast(t[k], s) for k in ("fg", "muted", "ta1", "ta2") for s in surfaces(t))
    large = min(contrast(c, s) for c in t["gstops"] for s in surfaces(t)) * AA / AA_LARGE
    return min(small, large)


# Synthwave "Mountain Pass": Audiowide display, Chakra Petch body, VT323 terminal.
THEME = theme(
    bg="#14002e", bg2="#0e0024", border="#6a22a3", panel="#1f0642", radius=16,
    fg="#fdf0ff", muted="#b490dc", a1="#00f0ff", a2="#ff2fd6", g1="#ff2fd6", g2="#00f0ff",
    font=f"'Chakra Petch',{SANS}", font_display="'Audiowide',sans-serif", font_mono=f"'VT323',{MONO}",
    mono_adv=0.4,  # VT323 advance width (em); typing caret maths depends on it
    fonts=[("Audiowide", 400, "audiowide-400.woff2"), ("Chakra Petch", 400, "chakra-petch-400.woff2"),
           ("Chakra Petch", 700, "chakra-petch-700.woff2"), ("VT323", 400, "vt323-400.woff2")],
    name_size=44, name_ls=4, name_ink=(3.16, 446.17),  # Audiowide ink edges of "PEDRO TORRES" at 44px + tracking 4
    type_fs=24,
)


# --- data --------------------------------------------------------------------

@dataclass(frozen=True)
class Stats:
    created: date
    last_year: int
    all_time: int
    commits: int
    prs: int
    issues: int
    files: int  # source files across owned repos, counted by scc
    loc: int  # code lines (no blanks or comments), counted by scc
    repos: int
    days: tuple[int, ...]  # daily counts, oldest first (rolling year)
    languages: tuple[tuple[str, str, float], ...]  # (name, color, percent)


def graphql(token: str, query: str, **variables: object) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "User-Agent": LOGIN},
    )
    body: dict = {}
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
            break
        except urllib.error.HTTPError as err:  # 5xx/rate-limit blips retry; auth or query errors fail fast
            if (err.code < 500 and err.code != 429) or attempt == RETRIES - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == RETRIES - 1:
                raise
        time.sleep(2 ** attempt * 5)
    if body.get("errors"):
        raise RuntimeError(f"GraphQL error: {body['errors']}")
    return body["data"]["user"]


def fetch_repos(token: str) -> list[dict]:
    """Owned, non-fork repos (public and private). nameWithOwner is used only to clone for the code count;
    it is never logged or rendered."""
    query = """query($login:String!,$after:String){user(login:$login){
      repositories(first:100,after:$after,ownerAffiliations:OWNER,isFork:false){
        pageInfo{hasNextPage endCursor}
        nodes{nameWithOwner languages(first:20){edges{size node{name color}}}}}}}"""
    nodes, after = [], None
    while True:
        page = graphql(token, query, login=LOGIN, after=after)["repositories"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def fetch_stats(token: str) -> Stats:
    user = graphql(token, """query($login:String!){user(login:$login){
      createdAt pullRequests{totalCount} issues{totalCount}
      contributionsCollection{contributionCalendar{totalContributions
        weeks{contributionDays{contributionCount}}}}}}""", login=LOGIN)
    created = datetime.fromisoformat(user["createdAt"]).date()
    years = " ".join(
        f'y{y}:contributionsCollection(from:"{y}-01-01T00:00:00Z",to:"{y}-12-31T23:59:59Z")'
        "{totalCommitContributions contributionCalendar{totalContributions}}"
        for y in range(created.year, datetime.now(UTC).year + 1)
    )
    per_year = graphql(token, f"query($login:String!){{user(login:$login){{{years}}}}}", login=LOGIN)
    repos = fetch_repos(token)
    files, loc = count_code(token, repos)
    calendar = user["contributionsCollection"]["contributionCalendar"]
    return Stats(
        created=created,
        last_year=calendar["totalContributions"],
        all_time=sum(v["contributionCalendar"]["totalContributions"] for v in per_year.values()),
        commits=sum(v["totalCommitContributions"] for v in per_year.values()),
        prs=user["pullRequests"]["totalCount"],
        issues=user["issues"]["totalCount"],
        files=files,
        loc=loc,
        repos=len(repos),
        days=tuple(d["contributionCount"] for w in calendar["weeks"] for d in w["contributionDays"]),
        languages=aggregate_languages(repos),
    )


def sum_scc(rows: list[dict]) -> tuple[int, int]:
    """(files, code lines) from scc's JSON, skipping markup/data/docs so only real source counts."""
    code = [r for r in rows if r["Name"] not in NON_CODE]
    return sum(r["Count"] for r in code), sum(r["Code"] for r in code)


def count_code(token: str, repos: list[dict]) -> tuple[int, int]:
    """Count source across every owned repo with scc. Each repo is partially cloned (files <= MAX_FILE only: bigger
    ones are data, binaries or generated output) into an index-named dir. The token travels in git's env config,
    never argv, and failures are counted, never echoed, so neither the token nor a repo name can reach a log."""
    if shutil.which("scc") is None:
        sys.exit("scc is not installed (https://github.com/boyter/scc); the workflow installs it.")
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}"}
    skipped = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, repo in enumerate(repos):
            if not _checkout_small_files(f"https://github.com/{repo['nameWithOwner']}.git", Path(tmp, str(i)), env):
                skipped += 1
        if skipped:
            print(f"code count: skipped {skipped} of {len(repos)} repos (empty or unavailable)", file=sys.stderr)
        out = subprocess.run(["scc", "--no-min-gen", "--format", "json", "--exclude-dir", SCC_EXCLUDE_DIRS, tmp],
                             capture_output=True, check=True, text=True, timeout=600)
    return sum_scc(json.loads(out.stdout))


def _checkout_small_files(url: str, dest: Path, env: dict[str, str]) -> bool:
    """Shallow, blob-filtered clone, then check out only the blobs that were downloaded. False on any failure."""
    def git(*args: str, stdin: bytes | None = None) -> bytes:
        return subprocess.run(["git", *args], env=env, input=stdin, capture_output=True, check=True, timeout=300).stdout

    try:
        git("clone", "--depth", "1", "--single-branch", "--no-checkout", "--quiet",
            f"--filter=blob:limit={MAX_FILE}", url, str(dest))
        entries = git("-C", str(dest), "ls-tree", "-r", "-l", "-z", "HEAD").split(b"\0")
        paths = [e.split(b"\t", 1)[1] for e in entries
                 if b"\t" in e and e.split()[1] == b"blob" and e.split()[3].isdigit() and int(e.split()[3]) <= MAX_FILE]
        if paths:
            git("-C", str(dest), "checkout", "HEAD", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=b"\0".join(paths))
        return True
    except (subprocess.SubprocessError, OSError, IndexError):  # never re-raise: the error would carry the repo URL
        return False


def aggregate_languages(
    repos: list[dict], exclude: frozenset[str] = EXCLUDED_LANGUAGES, top: int = TOP_LANGUAGES
) -> tuple[tuple[str, str, float], ...]:
    sizes: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name not in exclude:
                sizes[name] = sizes.get(name, 0) + edge["size"]
                colors[name] = edge["node"]["color"] or "#8b949e"
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])
    out = [(n, colors[n], 100 * s / total) for n, s in ranked[:top]]
    rest = sum(s for _, s in ranked[top:])
    if rest:
        out.append(("Other", "#8b949e", 100 * rest / total))
    return tuple(out)


# --- derived metrics -----------------------------------------------------------

def fmt_k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def streaks(days: tuple[int, ...]) -> tuple[int, int]:
    """(current, longest). Today counting zero doesn't break the current streak."""
    longest = run = 0
    for n in days:
        run = run + 1 if n else 0
        longest = max(longest, run)
    tail = days[:-1] if days and not days[-1] else days
    current = 0
    for n in reversed(tail):
        if not n:
            break
        current += 1
    return current, longest


def uptime(created: date, today: date) -> str:
    months = (today.year - created.year) * 12 + today.month - created.month
    return f"{months // 12}y {months % 12}m"


# --- svg helpers -----------------------------------------------------------------

def svg(height: int, t: dict, body: str, title: str, faces: frozenset[tuple[str, int]]) -> str:
    """Card shell: fonts, shared CSS, defs, background, body, and the border drawn last so scenes never cover it."""
    r = t["radius"]
    edge = f'x=".5" y=".5" width="{WIDTH - 1}" height="{height - 1}" rx="{r}"'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" fill="none" role="img" aria-labelledby="t">
<title id="t">{escape(title)}</title>
<style>
{font_faces(t, faces)}
.m{{font-family:{t['font_mono']};font-size-adjust:0.53}} .ty{{font-size-adjust:none}} .mu{{fill:{t['muted']}}} .rm{{display:none}}
@keyframes up{{from{{opacity:0;transform:translateY(8px)}}}}
@keyframes wide{{from{{transform:scaleX(0)}}}}
@keyframes on{{from{{opacity:0}}}}
@keyframes flash{{0%,100%{{opacity:1}}}}
.up{{animation:up .6s cubic-bezier(.2,.8,.2,1) both}} .on{{animation:on 0s linear both}}
.wide{{transform-box:fill-box;transform-origin:left;animation:wide .9s cubic-bezier(.2,.8,.2,1) both}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}} .tw{{display:none}} .rm{{display:inline}}}}
</style>
<defs>{gradient("g", t["gstops"])}
{gradient("bgg", (t["bg"], t["bg2"]), vertical=True)}
<filter id="glow" x="-10%" y="-60%" width="120%" height="220%"><feGaussianBlur stdDeviation="5" result="b"/>
<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<clipPath id="card"><rect width="{WIDTH}" height="{height}" rx="{r}"/></clipPath></defs>
<rect {edge} fill="url(#bgg)"/>
<g fill="{t['fg']}" font-family="{t['font']}">
{body}
</g>
<rect {edge} stroke="{t['border']}"/>
</svg>
"""


def font_faces(t: dict, faces: frozenset[tuple[str, int]]) -> str:
    """@font-face rules with the woff2 inlined (<img>-rendered SVG can't fetch fonts); only the faces a card uses."""
    return "".join(
        f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
        f"src:url(data:font/woff2;base64,{base64.b64encode((FONT_DIR / file).read_bytes()).decode()}) format('woff2')}}"
        for family, weight, file in t["fonts"] if (family, weight) in faces
    )


def gradient(gid: str, colors: Sequence[str], vertical: bool = False) -> str:
    end = 'x2="0" y2="1"' if vertical else 'x2="1" y2="0"'
    stops = "".join(f'<stop offset="{i / max(len(colors) - 1, 1):.2f}" stop-color="{c}"/>' for i, c in enumerate(colors))
    return f'<linearGradient id="{gid}" x1="0" y1="0" {end}>{stops}</linearGradient>'


def delay(seconds: float) -> str:
    return f' style="animation-delay:{seconds:.2f}s"'


def kicker(t: dict, text: str, x: float = MARGIN, anchor: str = "start") -> str:
    """Card label: one size, tracking and colour across all cards."""
    shift = {"middle": 1.5, "end": 3}.get(anchor, 0)  # letter-spacing trails the last glyph too
    align = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (f'<text class="m up" x="{x + shift:g}" y="{KICKER_Y}" font-size="13" letter-spacing="3"{align} '
            f'fill="{t["ta1"]}">{escape(text)}</text>')


def lang_fill(t: dict, color: str) -> str:
    """Linguist colour, lightened only as far as needed for 3:1 (WCAG 1.4.11 non-text contrast)."""
    return f'fill="{readable(color, surfaces(t), AA_LARGE)}"'


# --- header scene (clipped to the card) --------------------------------------------

def sun(t: dict, cx: float, cy: float, r: float) -> str:
    """Striped retro sun; stripes scroll down through the lower half."""
    stripes = "".join(f'<rect x="{cx - r}" y="{y}" width="{2 * r}" height="{2 + (y - cy) / 14:.1f}" fill="{t["bg2"]}"/>'
                      for y in range(int(cy) + 10, int(cy + r) + 24, 12))
    return ("<style>@keyframes slide{to{transform:translateY(12px)}}</style>"
            f'{gradient("sun", ("#ffd319", t["a2"]), vertical=True)}<clipPath id="sc"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#sun)"{GLOW}/>'
            f'<g clip-path="url(#sc)"><g style="animation:slide 1.6s linear infinite">{stripes}</g></g>')


def floor(t: dict, x0: float, horizon: float, vx: float, gap: int = 10, ray: int = 60) -> str:
    """Perspective grid floor whose horizontals roll toward the viewer."""
    rays = "".join(f'<line x1="{vx}" y1="{horizon}" x2="{vx + dx}" y2="200"/>' for dx in range(-1200, 1201, ray))
    rows = "".join(f'<line x1="{x0}" y1="{y}" x2="{WIDTH}" y2="{y}"/>' for y in range(int(horizon), 202 + gap, gap))
    h = 200 - horizon
    return (f"<style>@keyframes floor{{to{{transform:translateY({gap}px)}}}}</style>"
            f'<clipPath id="fc"><rect x="{x0}" y="{horizon}" width="{WIDTH - x0}" height="{h}"/></clipPath>'
            f'<g clip-path="url(#fc)"><rect x="{x0}" y="{horizon}" width="{WIDTH - x0}" height="{h}" fill="{t["bg2"]}"/>'
            f'<g stroke-opacity=".7"><g stroke="{t["a2"]}">{rays}</g>'
            f'<g stroke="{t["a2"]}" style="animation:floor 1s linear infinite">{rows}</g></g></g>')


RIDGES = ([(0.12, 30), (0.28, 12), (0.45, 52), (0.62, 20), (0.8, 44), (0.95, 16)],
          [(0.08, 10), (0.22, 26), (0.38, 6), (0.55, 30), (0.72, 8), (0.9, 22)])


def mountains(t: dict, horizon: float, x0: float) -> str:
    """Two wireframe ridges on the horizon, pink behind cyan."""
    span = WIDTH + 24 - x0
    return "".join(
        f'<polygon points="{x0 - 12:.0f},{horizon} '
        + " ".join(f"{x0 + fx * span:.0f},{horizon - fy:.0f}" for fx, fy in peaks)
        + f' {WIDTH + 12},{horizon}" fill="{t["bg2"]}" stroke="{color}" stroke-width="1.5"/>'
        for peaks, color in zip(RIDGES, (t["a2"], t["a1"]), strict=True)
    )


def scene(t: dict, cx: float = 690, cy: float = 110, r: float = 72, horizon: float = 160, fade: float = 120) -> str:
    """Sun, mountains and floor; the ground fades in from SAFE_X so its left edge never looks cropped."""
    ground = mountains(t, horizon, SAFE_X) + floor(t, SAFE_X, horizon, cx)
    return (sun(t, cx, cy, r)
            + f'<linearGradient id="fadeg" gradientUnits="userSpaceOnUse" x1="{SAFE_X}" y1="0" x2="{SAFE_X + fade}" y2="0">'
            '<stop offset="0" stop-color="#000"/><stop offset="1" stop-color="#fff"/></linearGradient>'
            f'<mask id="fade" maskUnits="userSpaceOnUse" x="0" y="0" width="{WIDTH}" height="200">'
            f'<rect width="{WIDTH}" height="200" fill="url(#fadeg)"/></mask>'
            f'<g mask="url(#fade)">{ground}</g>')


# --- header ------------------------------------------------------------------------

def typing_lines(stats: Stats) -> list[str]:
    """~76s typewriter script (17 lines x 4.5s). Public work only; private repos never named."""
    return [
        "Quant-minded engineer · In the Cloud",
        "Python for research. Rust for speed.",
        f"{fmt_k(stats.last_year)} contributions in the last 12 months",
        "Pair-programming with a swarm of AI agents",
        "Prompt › plan › ship › measure › repeat",  # › is in VT323; → is not
        "Certified AI whisperer (ask my agents)",
        "Context is the new compiler",
        "Agent-native tooling: CLI, API, MCP",
        "Teaching machines to read the tape",
        "Backtest first. Believe later.",
        "Baselines > vibes. Out-of-sample or bust.",
        "Solidity contracts that trust no one",
        "Red-teaming my own code before you do",
        "Grinding LeetCode between deploys",
        "Arduino sketches while the GPUs cool off",
        "Challenge me on Lichess. I dare you.",
        "Status: compiling the next big idea…",
    ]


def typing_svg(lines: list[str], t: dict, x0: float, y: float, fs: float, slot: float = 4.5) -> str:
    """SMIL typewriter: each line types, holds, erases in its own time slot. Under reduced motion the
    animated group is hidden and the first line shows statically (CSS can't pause SMIL)."""
    total, parts = slot * len(lines), []
    for i, line in enumerate(lines):
        n, w = len(line), len(line) * fs * t["mono_adv"]
        start, end = i / len(lines), (i + 1) / len(lines)
        type_end, erase_start, erase_end = (start + (end - start) * f for f in (0.35, 0.8, 0.95))
        times = [0, start] + [start + (type_end - start) * k / n for k in range(1, n + 1)]
        times += [erase_start + (erase_end - erase_start) * k / n for k in range(1, n + 1)] + [1]
        widths = [0.0, 0.0] + [w * k / n for k in range(1, n + 1)] + [w * (n - k) / n for k in range(1, n + 1)] + [0.0]
        kt = ";".join(f"{v:.4f}" for v in times)

        def anim(attr: str, vals: Sequence[float], kt: str = kt) -> str:
            return (f'<animate attributeName="{attr}" values="{";".join(f"{v:.1f}" for v in vals)}" '
                    f'keyTimes="{kt}" dur="{total}s" calcMode="discrete" repeatCount="indefinite"/>')

        parts.append(
            f'<clipPath id="c{i}"><rect x="{x0}" y="{y - 20}" width="0" height="28">{anim("width", widths)}</rect></clipPath>'
            f'<g opacity="{int(i == 0)}"><animate attributeName="opacity" values="0;1;0" '
            f'keyTimes="0;{start:.4f};{end:.4f}" dur="{total}s" calcMode="discrete" repeatCount="indefinite"/>'
            f'<text class="m ty" x="{x0}" y="{y}" font-size="{fs:g}" textLength="{w:.1f}" lengthAdjust="spacingAndGlyphs" '
            f'clip-path="url(#c{i})">{escape(line)}</text>'
            f'<rect x="{x0}" y="{y - 16}" width="9" height="20" fill="{t["a1"]}">{anim("x", [x0 + v for v in widths])}'
            f'<animate attributeName="opacity" values="1;0;1" dur="1s" calcMode="discrete" repeatCount="indefinite"/></rect></g>'
        )
    static = f'<text class="m ty rm" x="{x0}" y="{y}" font-size="{fs:g}">{escape(lines[0])}</text>'
    return f'<g class="tw">{"".join(parts)}</g>{static}'


def twin_stripes(t: dict, x: float, y: float, w: float) -> str:
    """Pink 2px over cyan 1px, 5px apart; the cyan one stops under the last R (83% of the name)."""
    return "".join(f'<rect class="wide" x="{x:.0f}" y="{y + k * 5}" width="{w * length:.0f}" height="{h}" fill="{fill}"'
                   f'{delay(0.35 + k * 0.15)}/>'
                   for k, (h, length, fill) in enumerate(((2, 1, t["a2"]), (1, 0.83, t["a1"]))))


def name_svg(t: dict, y: float, text: str = "PEDRO TORRES") -> str:
    """Gradient name with glow; first glyph's ink sits on the margin and the stripes span the exact ink width."""
    left, right = t["name_ink"]
    text_x = MARGIN - left
    name = (f'<text class="up" x="{text_x:.2f}" y="{y}" font-size="{t["name_size"]}" font-weight="400" '
            f'font-family="{t["font_display"]}" letter-spacing="{t["name_ls"]}" fill="url(#g)"{GLOW}{delay(0.1)}>{text}</text>')
    x0, x1 = round(text_x + left), round(text_x + right)
    return name + f'<g aria-hidden="true">{twin_stripes(t, x0, y + 7, x1 - x0)}</g>'


def render_header(stats: Stats, t: dict) -> str:
    """Name, twin stripes, subtitle and typewriter, centred vertically; the synthwave scene fills the right."""
    name_y = 76  # cap tops at 45, typing baseline at 151: equal space above and below the block
    tag_y = name_y + 39  # stripes end at +13, then 14px to the subtitle's cap tops
    type_y = tag_y + 36  # 20px from the subtitle baseline to the terminal caps
    lines = typing_lines(stats)
    # largest size <= type_fs that keeps the longest line left of the scene, never below 12px
    avail = SAFE_X - 10 - (MARGIN + 26)
    fs = max(12.0, min(t["type_fs"], avail / (max(map(len, lines)) * t["mono_adv"])))
    body = f"""<g clip-path="url(#card)" aria-hidden="true">{scene(t)}</g>
{name_svg(t, name_y)}
<text class="mu up" x="{MARGIN}" y="{tag_y}" font-size="17"{delay(0.6)}>{TAGLINE}</text>
<path d="M{MARGIN + 1} {type_y - 13}l6.5 6.5l-6.5 6.5" fill="none" stroke="{t['ta2']}" stroke-width="2.5" stroke-linecap="square" aria-hidden="true"/>
{typing_svg(lines, t, MARGIN + 26, type_y, fs)}"""
    title = f"Pedro Torres. {TAGLINE} " + " / ".join(lines)
    return svg(200, t, body, title, frozenset({("Audiowide", 400), ("Chakra Petch", 400), ("VT323", 400)}))


# --- stats ---------------------------------------------------------------------------

def count_up(t: dict, value: int, x: float, y: float, fmt: Callable[[int], str] = "{:,}".format,
             start: float = 0.0, frames: int = 10, step: float = 0.08, size: int = 56) -> str:
    """CSS-only count-up: pre-rendered frames flash in turn, the last one stays (and is the only one read aloud).
    Frames share the final value's measured left edge, so the settled number is exactly centred on x and
    Audiowide's proportional digits don't make it wobble while counting."""
    left = x - sum(DISPLAY_ADV.get(c, 0.75) for c in fmt(value)) * size / 2
    out = []
    for k in range(1, frames + 1):
        last = k == frames
        css = (f"animation-delay:{start + k * step:.2f}s" if last
               else f"opacity:0;animation:flash {step}s linear {start + k * step:.2f}s 1")
        val = round(value * (1 - (1 - k / frames) ** 3))
        cls, hidden = (' class="on"', "") if last else ("", ' aria-hidden="true"')  # only the settled value is read aloud
        out.append(f'<text{cls} x="{left:.1f}" y="{y}" font-size="{size}" '
                   f'font-family="{t["font_display"]}" fill="url(#g)" style="{css}"{hidden}>{fmt(val)}</text>')
    return f'<g{GLOW}>{"".join(out)}</g>'


def stat_row(stats: Stats, today: date) -> list[tuple[str, str]]:
    current, _ = streaks(stats.days)
    return [("STREAK", f"{current}d"), ("PRS", str(stats.prs)), ("ISSUES", str(stats.issues)),
            ("REPOS", str(stats.repos)), ("COMMITS", fmt_k(stats.commits)), ("UPTIME", uptime(stats.created, today))]


def render_stats(stats: Stats, t: dict, today: date) -> str:
    """Lines of code and files side by side, each centred in its half; six stats evenly spaced below."""
    mid = WIDTH / 2
    left, right, cell = mid - 190, mid + 190, (RIGHT - MARGIN) / 6
    cap = 'class="m up" y="134" font-size="13" letter-spacing="3" text-anchor="middle"'  # x +1.5: trailing tracking
    row = "".join(
        f'<g class="up"{delay(1.0 + i * 0.1)}><text class="m" x="{MARGIN + cell * (i + 0.5) + 1.5:g}" y="190" font-size="13" '
        f'letter-spacing="3" text-anchor="middle" fill="{(t["ta1"], t["ta2"])[i % 2]}">{label}</text>'
        # 18 x 0.53/0.4 renders 24px VT323, the same size as the typing line
        f'<text class="m" x="{MARGIN + cell * (i + 0.5):g}" y="216" font-size="18" text-anchor="middle">{value}</text></g>'
        for i, (label, value) in enumerate(stat_row(stats, today)))
    body = f"""{kicker(t, "TOTAL CODE", mid, "middle")}
{count_up(t, stats.loc, left, 106, fmt_k)}{count_up(t, stats.files, right, 106, start=0.3)}
<text {cap} x="{left + 1.5:g}" fill="{t['ta1']}"{delay(0.9)}>LINES OF CODE</text>
<text {cap} x="{right + 1.5:g}" fill="{t['ta2']}"{delay(0.9)}>FILES</text>
{gradient("gv", t["gstops"], vertical=True)}<rect x="{mid:g}" y="63" width="1" height="74" fill="url(#gv)" opacity=".8"/>
<rect x="{MARGIN}" y="160" width="{RIGHT - MARGIN}" height="1" fill="{t['border']}"/>
{row}"""
    title = (f"{stats.loc:,} lines of code in {stats.files:,} files across {stats.repos} original repos. "
             + ", ".join(f"{label.lower()} {value}" for label, value in stat_row(stats, today)) + ".")
    return svg(248, t, body, title, frozenset({("Audiowide", 400), ("VT323", 400)}))


# --- languages ---------------------------------------------------------------------

def render_languages(stats: Stats, t: dict) -> str:
    """8px stacked bar, then two columns of name / 3px bar scaled to the top language / percent."""
    langs, span, col = stats.languages, RIGHT - MARGIN, (RIGHT - MARGIN - 40) / 2
    bar, cum = [], 0.0
    for i, (_, color, pct) in enumerate(langs):
        x0 = MARGIN + round(span * cum / 100)  # whole-pixel edges: no anti-aliased seams between segments
        cum += pct
        x1 = MARGIN + round(span * cum / 100)
        bar.append(f'<rect class="wide" x="{x0}" y="64" width="{x1 - x0}" height="8" {lang_fill(t, color)}{delay(i * 0.1)}/>')
    rows, track = [], col - 200
    for i, (name, color, pct) in enumerate(langs):
        cx, cy = MARGIN + (i % 2) * (col + 40), 114 + (i // 2) * 34
        rows.append(
            f'<g class="up"{delay(0.5 + i * 0.07)}><circle cx="{cx + 5:.0f}" cy="{cy - 5}" r="5" {lang_fill(t, color)}/>'
            f'<text x="{cx + 20:.0f}" y="{cy}" font-size="14" font-weight="700">{escape(name)}</text>'
            f'<text class="m mu" x="{cx + col:.0f}" y="{cy}" font-size="13" text-anchor="end">{pct:.1f}%</text>'
            f'<rect x="{cx + 130:.0f}" y="{cy - 7}" width="{track:.0f}" height="3" rx="1.5" fill="{t["panel"]}"/>'
            f'<rect class="wide" x="{cx + 130:.0f}" y="{cy - 7}" width="{max(track * pct / langs[0][2], 3):.1f}" '
            f'height="3" rx="1.5" {lang_fill(t, color)}{delay(0.7 + i * 0.07)}/></g>'
        )
    height = 114 + ((len(langs) + 1) // 2 - 1) * 34 + 32  # last baseline + 32px
    body = f"""{kicker(t, "LANGUAGES")}
<clipPath id="bar"><rect x="{MARGIN}" y="64" width="{span}" height="8" rx="4"/></clipPath>
<g clip-path="url(#bar)" aria-hidden="true">{"".join(bar)}</g>
{"".join(rows)}"""
    title = f"Languages by bytes across {stats.repos} original repos: " + ", ".join(f"{n} {p:.1f}%" for n, _, p in langs) + "."
    return svg(height, t, body, title, frozenset({("Chakra Petch", 700), ("VT323", 400)}))


# --- main --------------------------------------------------------------------------

def render_all(stats: Stats, today: date, t: dict = THEME) -> dict[str, str]:
    return {
        "header.svg": render_header(stats, t),
        "stats.svg": render_stats(stats, t, today),
        "languages.svg": render_languages(stats, t),
    }


def main() -> None:
    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("GH_TOKEN is not set (needs a token for the profile owner).")
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "assets")
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = fetch_stats(token)
    for name, content in render_all(stats, datetime.now(UTC).date()).items():
        (out_dir / name).write_text(content, encoding="utf-8")
    print(f"wrote {out_dir}: {stats.last_year} last-year, {stats.all_time} all-time, {stats.commits} commits, {stats.files} files, {stats.loc} loc, {stats.repos} repos")


if __name__ == "__main__":
    main()
