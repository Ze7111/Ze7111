#!/usr/bin/env python3
"""
Generates the SVG cards used by the profile README.

  header-{theme}.svg    ASCII-bitmap name banner + status strip
  deck-{theme}.svg      selected work, one row per domain
  waka-{theme}.svg      WakaTime breakdown, segmented terminal bars
  pipeline-{theme}.svg  Kairo compiler stages (for the kairo repo, not here)

Design constraints (load-bearing, do not "simplify"):
  * No background rect. GitHub's own page background shows through.
  * Two files per card, one palette each, selected with <picture>.
    A prefers-color-scheme query *inside* an <img>-embedded SVG follows the
    OS, not GitHub's theme toggle, so it desyncs. <picture> is the only
    mechanism GitHub actually drives.
  * Only transform / opacity / stroke-dashoffset are animated. SMIL and
    CSS-animated geometry attributes are unreliable in an <img> context.
  * ASCII art is rasterised to <rect>, never drawn as text. Text-based
    ASCII depends on whichever monospace font the viewer resolves and the
    letterforms collapse when the metrics are off.
  * Static state lives in attributes, CSS carries only motion. Any renderer
    that ignores CSS shows a finished card, not an empty one.
  * prefers-reduced-motion kills every animation and shows the end state.

stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

WAKA_API = "https://wakatime.com/api/v1/users/current/stats/{range}"

THEMES = {
    "dark": {
        "fg": "#e6edf3", "muted": "#8b949e", "faint": "#484f58",
        "line": "#30363d", "track": "#1c2128",
        "accent": "#b8ff2f", "accent2": "#7ee787",
        "ok": "#3fb950", "warn": "#d29922",
    },
    "light": {
        "fg": "#1f2328", "muted": "#59636e", "faint": "#818b98",
        "line": "#d1d9e0", "track": "#eaeef2",
        "accent": "#4d7c0f", "accent2": "#3f6212",
        "ok": "#1a7f37", "warn": "#9a6700",
    },
}

MONO = ("ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,"
        "'DejaVu Sans Mono','Liberation Mono',monospace")

W = 860

# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

BLOCK_FONT = {
    "A": [" ####  ", "##  ## ", "###### ", "##  ## ", "##  ## "],
    "D": ["#####  ", "##  ## ", "##  ## ", "##  ## ", "#####  "],
    "H": ["##  ## ", "##  ## ", "###### ", "##  ## ", "##  ## "],
    "I": ["## ", "## ", "## ", "## ", "## "],
    "K": ["##  ## ", "## ##  ", "####   ", "## ##  ", "##  ## "],
    "N": ["##  ## ", "### ## ", "###### ", "## ### ", "##  ## "],
    "O": [" ####  ", "##  ## ", "##  ## ", "##  ## ", " ####  "],
    "R": ["#####  ", "##  ## ", "#####  ", "##  ## ", "##  ## "],
    "U": ["##  ## ", "##  ## ", "##  ## ", "##  ## ", " ####  "],
    "V": ["##  ## ", "##  ## ", "##  ## ", " ####  ", "  ##   "],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def banner(word: str) -> list[str]:
    glyphs = [BLOCK_FONT[c] for c in word.upper()]
    rows = ["".join(g[r] for g in glyphs).rstrip() for r in range(5)]
    w = max(len(r) for r in rows)
    return [r.ljust(w) for r in rows]


def mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def label(text: str, x: float, y: float, c: dict, anchor: str = "start",
          size: float = 9.0, cls: str = "fa") -> str:
    """Uppercase micro-label with tracking. The telemetry look lives here."""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
            f'letter-spacing="1.6" text-anchor="{anchor}" class="{cls}">'
            f'{esc(text.upper())}</text>')


def txt(text: str, x: float, y: float, size: float = 12.0, cls: str = "fg",
        anchor: str = "start", fill: str | None = None) -> str:
    f = f' fill="{fill}"' if fill else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
            f'text-anchor="{anchor}" class="{cls}"{f}>{esc(text)}</text>')


def rule(y: float, c: dict, x1: float = 0.0, x2: float = float(W),
         cls: str = "") -> str:
    k = f' class="{cls}"' if cls else ""
    return (f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" '
            f'stroke="{c["line"]}" stroke-width="1"{k}/>')


def ticks(x0: float, y0: float, x1: float, y1: float, c: dict,
          n: float = 7.0) -> str:
    """Corner registration marks. Cheap, and they carry the whole aesthetic."""
    out = []
    for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                           (x0, y1, 1, -1), (x1, y1, -1, -1)):
        out.append(f'<path d="M{cx} {cy + dy * n} V{cy} H{cx + dx * n}" '
                   f'fill="none" stroke="{c["faint"]}" stroke-width="1"/>')
    return "".join(out)


def shell(height: int, body: str, style: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
        f'viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}" '
        f'font-family="{MONO}"><title>{esc(title)}</title>'
        f"<style>{style}</style>{body}</svg>"
    )


BASE = """
text{{dominant-baseline:middle;white-space:pre}}
.fg{{fill:{fg}}} .mu{{fill:{muted}}} .fa{{fill:{faint}}}
.ac{{fill:{accent}}} .ok{{fill:{ok}}} .wn{{fill:{warn}}}
"""

REDUCED = """
@media (prefers-reduced-motion:reduce){
  *{animation:none!important}
  .px,.cell,.row,#reveal,#caret{opacity:1!important;transform:none!important}
  .rule{stroke-dashoffset:0!important}
  .rm-hide{display:none}
}
"""

# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------


def render_header(theme: str, name: str, tagline: str, strip) -> str:
    c = THEMES[theme]
    rows = banner(name)
    cols = len(rows[0])
    px, gap = 13.0, 2.0
    x0, y0 = 22.0, 42.0
    bw, bh = cols * px, 5 * px

    pix = "".join(
        f'<rect x="{x0 + q * px:.1f}" y="{y0 + r * px:.1f}" '
        f'width="{px - gap:.1f}" height="{px - gap:.1f}" '
        f'fill="{mix(c["accent"], c["accent2"], q / (cols - 1) * 0.75 + r / 14)}" '
        f'class="px" style="animation-delay:{q * 0.014 + r * 0.009:.3f}s"/>'
        for r, row in enumerate(rows) for q, ch in enumerate(row) if ch == "#"
    )
    maskpix = pix.replace('class="px"', 'class="pxm"')

    tl_y = y0 + bh + 30
    tl_size = 13.0
    tl_span = len(tagline) * tl_size * 0.601

    sy = tl_y + 40
    height = int(sy + 26)

    col_w = (W - 2 * x0) / len(strip)
    strip_svg = "".join(
        label(k, x0 + i * col_w, sy - 13, c)
        + txt(v, x0 + i * col_w, sy + 4, 12.0, tone)
        for i, (k, v, tone) in enumerate(strip)
    )

    style = BASE.format(**c) + f"""
.px{{transform-box:fill-box;transform-origin:center;
  animation:rake .45s cubic-bezier(.2,.9,.3,1) both}}
@keyframes rake{{from{{opacity:0;transform:scale(.15)}}
  to{{opacity:1;transform:scale(1)}}}}
#sweep{{animation:sweep 6s ease-in-out 1.6s infinite}}
@keyframes sweep{{0%{{transform:translateX(0)}}
  55%,100%{{transform:translateX({bw + 320:.0f}px)}}}}
#reveal{{animation:type 2.4s steps({len(tagline)},end) .8s both}}
#caret{{animation:type 2.4s steps({len(tagline)},end) .8s both,
  blink 1.05s steps(1,end) infinite}}
@keyframes type{{from{{transform:translateX(-{tl_span:.1f}px)}}
  to{{transform:translateX(0)}}}}
@keyframes blink{{50%{{opacity:0}}}}
.rule{{stroke-dasharray:{W};stroke-dashoffset:{W};
  animation:draw 1.5s ease-out .3s forwards}}
@keyframes draw{{to{{stroke-dashoffset:0}}}}
{REDUCED}"""

    defs = f"""<defs>
<linearGradient id="shine" x1="0" y1="0" x2="1" y2="0">
  <stop offset="0" stop-color="#fff" stop-opacity="0"/>
  <stop offset=".5" stop-color="#fff" stop-opacity="{.55 if theme == 'dark' else .8}"/>
  <stop offset="1" stop-color="#fff" stop-opacity="0"/>
</linearGradient>
<mask id="bm"><g fill="#fff">{maskpix}</g></mask>
<clipPath id="clip"><rect id="reveal" x="{x0}" y="{tl_y - 10}"
  width="{tl_span:.1f}" height="20"/></clipPath></defs>"""

    body = [
        defs,
        ticks(8, 8, W - 8, height - 8, c),
        label("dhruvan kartik", x0, 22, c),
        label("fort wayne, indiana", W - x0, 22, c, anchor="end"),
        pix,
        f'<g mask="url(#bm)" class="rm-hide"><rect id="sweep" x="{x0 - 180}" '
        f'y="{y0 - 10}" width="130" height="{bh + 20:.0f}" fill="url(#shine)"/></g>',
        f'<g clip-path="url(#clip)">{txt(tagline, x0, tl_y, tl_size, "mu")}</g>',
        f'<g clip-path="url(#clip)"><rect id="caret" x="{x0 + tl_span:.1f}" '
        f'y="{tl_y - 7:.1f}" width="7" height="14" fill="{c["accent"]}"/></g>',
        rule(sy - 28, c, x0, W - x0, cls="rule"),
        strip_svg,
    ]
    return shell(height, "".join(body), style, f"{name} - profile header")


# --------------------------------------------------------------------------
# work deck
# --------------------------------------------------------------------------


def render_deck(theme: str, items, note: str) -> str:
    """items: (domain, description, status, tone)."""
    c = THEMES[theme]
    x0 = 22.0
    dom_x, desc_x, st_x = x0 + 32, x0 + 172, float(W - x0)
    row_h, y0 = 26.0, 62.0
    height = int(y0 + len(items) * row_h + 36)
    tone_cls = {"live": "ac", "done": "ok", "wip": "wn", "off": "mu"}

    rows = []
    for i, (dom, desc, st, tone) in enumerate(items):
        y = y0 + i * row_h
        lead_x1 = desc_x + len(desc) * 7.15 + 12
        lead = (f'<line x1="{lead_x1:.0f}" y1="{y}" x2="{st_x - 66:.0f}" '
                f'y2="{y}" stroke="{c["faint"]}" stroke-width="1" '
                f'stroke-dasharray="1 4" opacity=".7"/>'
                if lead_x1 < st_x - 74 else "")
        rows.append(
            f'<g class="row" style="animation-delay:{i * 0.07:.2f}s">'
            + txt(f"{i + 1:02d}", x0, y, 10.0, "fa")
            + label(dom, dom_x, y, c, size=9.5, cls="mu")
            + txt(desc, desc_x, y, 12.5, "fg") + lead
            + label(st, st_x, y, c, anchor="end", size=9.5, cls=tone_cls[tone])
            + "</g>")

    style = BASE.format(**c) + f"""
.row{{animation:in .5s ease-out both}}
@keyframes in{{from{{opacity:0;transform:translateX(-6px)}}
  to{{opacity:1;transform:translateX(0)}}}}
{REDUCED}"""

    body = [
        ticks(8, 8, W - 8, height - 8, c),
        label("selected work", x0, 26, c, size=10, cls="fg"),
        label(f"{len(items)} active threads", W - x0, 26, c, anchor="end",
              size=9.5),
        rule(42, c, x0, W - x0),
        "".join(rows),
        rule(height - 30, c, x0, W - x0),
        label(note, x0, height - 16, c, size=9),
    ]
    return shell(height, "".join(body), style, "Selected work")


# --------------------------------------------------------------------------
# kairo pipeline (belongs in the kairo repo, not the profile)
# --------------------------------------------------------------------------


def render_pipeline(theme: str, stages, caption: str) -> str:
    c = THEMES[theme]
    n = len(stages)
    x0, arrow = 22.0, 26.0
    span = W - 2 * x0
    bwd = (span - (n - 1) * arrow) / n
    bh, y0 = 42.0, 50.0
    fill = {"done": c["ok"], "wip": c["accent"], "todo": c["faint"]}

    boxes, arrows, labels, pulses = [], [], [], []
    for i, (name, sub, st) in enumerate(stages):
        bx = x0 + i * (bwd + arrow)
        col = fill[st]
        dash = ' stroke-dasharray="4 3"' if st == "todo" else ""
        boxes.append(f'<rect x="{bx:.1f}" y="{y0}" width="{bwd:.1f}" '
                     f'height="{bh}" fill="none" stroke="{col}" '
                     f'stroke-opacity="{.85 if st != "todo" else .5}" '
                     f'stroke-width="1"{dash}/>')
        pulses.append(f'<rect class="pulse rm-hide" '
                      f'style="animation-delay:{i * 0.9:.2f}s" x="{bx:.1f}" '
                      f'y="{y0}" width="{bwd:.1f}" height="{bh}" '
                      f'fill="{col}" opacity="0"/>')
        labels.append(txt(name, bx + bwd / 2, y0 + 16, 13.0, "fg",
                          anchor="middle", fill=col)
                      + label(sub, bx + bwd / 2, y0 + 31, c,
                              anchor="middle", size=8.5))
        if i:
            my = y0 + bh / 2
            arrows.append(
                f'<path class="flow" style="animation-delay:'
                f'{i * 0.9 - 0.45:.2f}s" d="M{bx - arrow + 3:.1f} {my} '
                f'H{bx - 7:.1f}" stroke="{c["faint"]}" stroke-width="1.4" '
                f'fill="none"/>'
                f'<path d="M{bx - 7:.1f} {my - 3.5} L{bx - 1:.1f} {my} '
                f'L{bx - 7:.1f} {my + 3.5} Z" fill="{c["faint"]}"/>')

    height = int(y0 + bh + 44)
    dur = 0.9 * n + 1.4
    style = BASE.format(**c) + f"""
.pulse{{animation:pulse {dur}s ease-out infinite}}
@keyframes pulse{{0%{{opacity:0}}5%{{opacity:.16}}22%{{opacity:0}}100%{{opacity:0}}}}
.flow{{stroke-dasharray:4 4;animation:march {dur}s linear infinite}}
@keyframes march{{to{{stroke-dashoffset:-{arrow * 3:.0f}}}}}
{REDUCED}"""

    body = [
        ticks(8, 8, W - 8, height - 8, c),
        label("kcc - stage 1 front end", x0, 26, c, size=10, cls="fg"),
        label("self-hosted, bootstrapped by stage 0", W - x0, 26, c,
              anchor="end", size=9.5),
        "".join(pulses), "".join(arrows), "".join(boxes), "".join(labels),
        label(caption, x0, height - 18, c, size=9),
    ]
    return shell(height, "".join(body), style, "Kairo compiler pipeline")


# --------------------------------------------------------------------------
# wakatime
# --------------------------------------------------------------------------


def fetch_waka(key: str, rng: str) -> dict:
    import base64
    req = urllib.request.Request(
        WAKA_API.format(range=rng),
        headers={"Authorization": "Basic " + base64.b64encode(
            key.encode()).decode(), "User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def hms(sec: float) -> str:
    h, m = divmod(int(sec) // 60, 60)
    return f"{h:,} hrs {m} mins"


def normalise(data: dict, top: int, ignore: set[str]) -> dict:
    """Drop ignored languages, then re-derive percentages so the remaining rows
    still sum to 100. WakaTime's own `percent` is computed before any filtering,
    so reusing it after dropping rows yields bars that don't add up."""
    langs = [l for l in data.get("languages", [])
             if l.get("total_seconds", 0) > 0
             and l["name"].casefold() not in ignore]
    kept = sum(l["total_seconds"] for l in langs) or 1.0
    for l in langs:
        l["percent"] = l["total_seconds"] / kept * 100.0
    langs.sort(key=lambda l: -l["total_seconds"])
    keep, rest = langs[:top], langs[top:]
    out = [{"name": l["name"], "pct": l["percent"],
            "text": l.get("text") or hms(l["total_seconds"])} for l in keep]
    if rest:
        s = sum(l["total_seconds"] for l in rest)
        out.append({"name": f"+{len(rest)} more", "pct": s / kept * 100.0,
                    "text": hms(s)})
    return {"langs": out, "total": hms(kept),
            "start": (data.get("start") or "")[:10],
            "end": (data.get("end") or "")[:10],
            "range": data.get("range", "")}


def render_waka(theme: str, d: dict, cells: int = 46) -> str:
    c = THEMES[theme]
    langs = d["langs"]
    x0 = 22.0
    bar_x, bar_w = x0 + 148, 372.0
    time_x, pct_x = bar_x + bar_w + 28, float(W - x0)
    row_h, y0 = 26.0, 76.0
    height = int(y0 + len(langs) * row_h + 34)
    cw = bar_w / cells
    top = max((l["pct"] for l in langs), default=1.0) or 1.0

    rows = []
    for i, l in enumerate(langs):
        y = y0 + i * row_h
        filled = max(1, round(l["pct"] / top * cells))
        seq = "".join(
            f'<rect class="cell" style="animation-delay:'
            f'{i * 0.05 + k * 0.006:.3f}s" x="{bar_x + k * cw:.2f}" '
            f'y="{y - 5:.1f}" width="{cw - 1.4:.2f}" height="10" '
            f'fill="{c["accent"] if k < filled else c["track"]}"/>'
            for k in range(cells))
        rows.append(txt(l["name"][:17], x0, y, 12.5, "fg") + seq
                    + txt(l["text"], time_x, y, 11.5, "mu")
                    + txt(f'{l["pct"]:.2f}%', pct_x, y, 11.5, "ac",
                          anchor="end"))

    rng = f'{d["start"]} / {d["end"]}' if d["start"] else d["range"]
    style = BASE.format(**c) + f"""
.cell{{transform-box:fill-box;transform-origin:center;
  animation:pop .3s ease-out both}}
@keyframes pop{{from{{opacity:0;transform:scaleY(.2)}}
  to{{opacity:1;transform:scaleY(1)}}}}
{REDUCED}"""

    body = [
        ticks(8, 8, W - 8, height - 8, c),
        label("wakatime", x0, 26, c, size=10, cls="fg"),
        label("tracked total", pct_x, 26, c, anchor="end", size=9.5),
        txt(rng, x0, 46, 11.5, "fa"),
        txt(d["total"], pct_x, 46, 13.0, "ac", anchor="end"),
        rule(60, c, x0, W - x0),
        "".join(rows),
        label(f'generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} utc'
              f' · scripts/gen_cards.py', x0, height - 16, c, size=9),
    ]
    return shell(height, "".join(body), style, "WakaTime language breakdown")



# --------------------------------------------------------------------------
# profile: ASCII portrait terminal + telemetry panel
# --------------------------------------------------------------------------

GQL = """query($u:String!){user(login:$u){
  followers{totalCount}
  repositories(first:100,ownerAffiliations:[OWNER],isFork:false){
    totalCount nodes{stargazerCount}}
  contributionsCollection{contributionCalendar{totalContributions}}}}"""


def fetch_github(user: str, token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": GQL, "variables": {"u": user}}).encode(),
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=30) as r:
        u = json.load(r)["data"]["user"]
    return {
        "followers": u["followers"]["totalCount"],
        "repos": u["repositories"]["totalCount"],
        "stars": sum(n["stargazerCount"] for n in u["repositories"]["nodes"]),
        "contributions":
            u["contributionsCollection"]["contributionCalendar"]
            ["totalContributions"],
    }


def fetch_avatar(user: str, dest: pathlib.Path, size: int = 240) -> bool:
    """github.com/<user>.png redirects to the avatar CDN. Cached to disk so
    an offline run still renders a portrait."""
    try:
        req = urllib.request.Request(f"https://github.com/{user}.png?size={size}",
                                     headers={"User-Agent": "profile-cards"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:                                # noqa: BLE001
        print(f"warn: avatar fetch failed ({e})", file=sys.stderr)
        return False


RAMP = " .`:-=+*coaO#%@"


def ascii_from_image(path: pathlib.Path, cols: int, rows: int,
                     invert: bool) -> list[str]:
    """Luminance -> glyph density. On a dark card a bright pixel needs the
    denser glyph; on a light card it's the reverse, hence `invert`."""
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        im = Image.open(path).convert("L").resize((cols, rows), Image.LANCZOS)
    except Exception as e:                                # noqa: BLE001
        print(f"warn: avatar decode failed ({e})", file=sys.stderr)
        return []
    px = list(im.getdata()) if not hasattr(im, "get_flattened_data") else list(im.get_flattened_data())
    sp = sorted(px)
    lo, hi = sp[len(sp) // 20], sp[-max(1, len(sp) // 20)]
    span = max(1, hi - lo)
    out = []
    for r in range(rows):
        line = []
        for q in range(cols):
            v = (px[r * cols + q] - lo) / span
            if invert:
                v = 1.0 - v
            line.append(RAMP[min(len(RAMP) - 1, int(v * len(RAMP)))])
        out.append("".join(line))
    return out


def _bar(x: float, y: float, w: float, frac: float, c: dict,
         cells: int = 18) -> str:
    cw = w / cells
    on = max(1, round(frac * cells))
    return "".join(
        f'<rect x="{x + k * cw:.2f}" y="{y - 4:.1f}" width="{cw - 1.3:.2f}" '
        f'height="8" fill="{c["accent"] if k < on else c["track"]}"/>'
        for k in range(cells))


def render_profile(theme: str, user: str, art: list[str], gh: dict,
                   env: dict, projects: list[tuple[str, float, str]]) -> str:
    c = THEMES[theme]
    pad = 18.0
    art_w = 260.0
    panel_w = art_w + pad * 2
    cols = len(art[0]) if art else 0
    rows = len(art)
    cell_h = (art_w / rows) if rows else 0   # keeps the art square
    title_h, status_h = 30.0, 32.0
    art_h = rows * cell_h
    height = int(title_h + 10 + art_h + 10 + status_h)

    # ---- left: terminal window ------------------------------------------
    left = [
        f'<rect x="0.5" y="0.5" width="{panel_w - 1:.1f}" '
        f'height="{height - 1}" fill="none" stroke="{c["line"]}" '
        f'stroke-width="1"/>',
        f'<line x1="0" y1="{title_h}" x2="{panel_w:.1f}" y2="{title_h}" '
        f'stroke="{c["line"]}"/>',
    ]
    for i, dot in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        left.append(f'<circle cx="{pad + i * 15}" cy="{title_h / 2}" r="4.5" '
                    f'fill="{dot}"/>')
    left.append(label(f"{user}@github: ./portrait", panel_w / 2 + 18,
                      title_h / 2, c, anchor="middle", size=8.5))

    art_top = title_h + 10
    for r, line in enumerate(art):
        y = art_top + r * cell_h + cell_h * 0.72
        row = (f'<text xml:space="preserve" x="{pad}" y="{y:.2f}" '
               f'font-size="{cell_h:.2f}" fill="{c["fg"]}" '
               f'textLength="{art_w}" lengthAdjust="spacing" '
               f'opacity="{0.72 + 0.28 * (r / max(1, rows)):.2f}">'
               f'{esc(line)}</text>')
        left.append(
            f'<clipPath id="pr{r}"><rect class="scan" '
            f'style="animation-delay:{r * 0.055:.3f}s" x="{pad}" '
            f'y="{art_top + r * cell_h:.2f}" width="{art_w}" '
            f'height="{cell_h + 1:.2f}"/></clipPath>'
            f'<g clip-path="url(#pr{r})">{row}</g>')

    sy = height - status_h
    left.append(f'<line x1="0" y1="{sy}" x2="{panel_w:.1f}" y2="{sy}" '
                f'stroke="{c["line"]}"/>')
    left.append(txt(f"$ whoami", pad, sy + status_h / 2, 11.0, "fa"))
    left.append(txt(user, pad + 74, sy + status_h / 2, 11.0, "ac"))
    left.append(f'<rect id="blink" x="{pad + 74 + len(user) * 6.6:.1f}" '
                f'y="{sy + status_h / 2 - 6:.1f}" width="6" height="12" '
                f'fill="{c["accent"]}"/>')

    # ---- right: telemetry -----------------------------------------------
    rx = panel_w + 26
    rw = W - rx - 22
    right, y = [], 20.0

    def section(name: str, extra: str = "") -> None:
        nonlocal y
        right.append(label(name, rx, y, c, size=9.5, cls="fg"))
        if extra:
            right.append(label(extra, rx + rw, y, c, anchor="end", size=9))
        right.append(rule(y + 12, c, rx, rx + rw))
        y += 30

    def row(k: str, v: str, tone: str = "fg") -> None:
        nonlocal y
        right.append(label(k, rx, y, c, size=9, cls="mu"))
        right.append(txt(v, rx + rw, y, 12.0, tone, anchor="end"))
        vx = rx + rw - len(v) * 7.2 - 10
        kx = rx + len(k) * 6.6 + 12
        if kx < vx:
            right.append(f'<line x1="{kx:.0f}" y1="{y}" x2="{vx:.0f}" '
                         f'y2="{y}" stroke="{c["faint"]}" stroke-width="1" '
                         f'stroke-dasharray="1 4" opacity=".6"/>')
        y += 22

    section("github", f"@{user}")
    row("repositories", str(gh.get("repos", "—")))
    row("stars earned", str(gh.get("stars", "—")), "ac")
    row("followers", str(gh.get("followers", "—")))
    row("contributions · 12mo", f'{gh.get("contributions", "—"):,}'
        if isinstance(gh.get("contributions"), int) else "—", "ac")
    y += 8
    section("environment", env.get("range", ""))
    row("editor", env.get("editor", "—"))
    row("os", env.get("os", "—"))
    row("daily average", env.get("daily", "—"))
    y += 10
    right.append(label("time by project", rx, y, c, size=9.5, cls="fg"))
    right.append(rule(y + 12, c, rx, rx + rw))
    y += 30
    bar_x = rx + 132
    bar_w = rw - 132 - 56
    for name, frac, pct in projects[:4]:
        right.append(txt(name[:16], rx, y, 11.5, "fg"))
        right.append(_bar(bar_x, y, bar_w, frac, c))
        right.append(txt(pct, rx + rw, y, 11.0, "mu", anchor="end"))
        y += 21

    height = max(height, int(y + 14))

    style = BASE.format(**c) + """
.scan{transform-box:fill-box;transform-origin:left center;
  animation:scan .35s ease-out both}
@keyframes scan{from{transform:scaleX(0)}to{transform:scaleX(1)}}
#blink{animation:blink 1.05s steps(1,end) infinite}
@keyframes blink{50%{opacity:0}}
""" + REDUCED.replace(".px,.cell,.row,", ".px,.cell,.row,.scan,")

    body = ticks(8, 8, W - 8, height - 8, c) + "".join(left) + "".join(right)
    return shell(height, body, style, f"{user} - profile telemetry")


# --------------------------------------------------------------------------

STRIP = [
    ("focus", "compilers & systems", "ac"),
    ("upstream", "llvm / clang", "ok"),
    ("also", "graphics · ml infra · web", "mu"),
    ("degree", "purdue fw · may 2027", "mu"),
]

DECK = [
    ("compilers", "kairo - statically typed systems language, self-hosted",
     "active", "live"),
    ("upstream", "clang: lexer patch for prebuilt token injection",
     "merged", "done"),
    ("xr / graphics", "vr forklift training simulator for general motors",
     "team lead", "live"),
    ("ml infra", "dual-gpu local inference · llama.cpp · quantised models",
     "running", "done"),
    ("ai tooling", "kai - shell interceptor, local model command synthesis",
     "alpha", "wip"),
    ("web", "receipt-splitting pwa · next.js / neon / r2 / vision ocr",
     "beta", "wip"),
]

STAGES = [("lex", "tokens", "done"), ("pp", "wave-parallel", "done"),
          ("parse", "two-pass ast", "done"), ("sema", "name res", "wip"),
          ("cgen", "clang tokens", "wip"), ("kld", "link", "todo")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="assets")
    ap.add_argument("--cache", default="data/waka.json")
    ap.add_argument("--range", default=os.environ.get("WAKA_RANGE", "all_time"))
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--ignore", default=os.environ.get("WAKA_IGNORE",
                                                       "yaml,json"))
    ap.add_argument("--user", default=os.environ.get("GH_USER", "ze7111"))
    ap.add_argument("--portrait-invert", choices=("auto", "yes", "no"),
                    default="auto",
                    help="ink density follows darkness (auto: on light cards "
                         "only). Flip it if your avatar has a dark background "
                         "and the light card fills with ink.")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = pathlib.Path(args.cache)

    key = os.environ.get("WAKATIME_API_KEY", "")
    data = None
    if key and not args.offline:
        try:
            data = fetch_waka(key, args.range)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, indent=1))
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
            print(f"warn: wakatime fetch failed ({e}); using cache",
                  file=sys.stderr)
    if data is None:
        if not cache.exists():
            print("error: no API key and no cache", file=sys.stderr)
            return 1
        data = json.loads(cache.read_text())

    ignore = {x.strip().casefold() for x in args.ignore.split(",") if x.strip()}
    d = normalise(data, args.top, ignore)

    # --- github counters: live if a token is present, else last good pull ---
    ghc = pathlib.Path(args.cache).with_name("github.json")
    gh = {}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok and not args.offline:
        try:
            gh = fetch_github(args.user, tok)
            ghc.write_text(json.dumps(gh, indent=1))
        except Exception as e:                            # noqa: BLE001
            print(f"warn: github fetch failed ({e}); using cache",
                  file=sys.stderr)
    if not gh and ghc.exists():
        gh = json.loads(ghc.read_text())

    # --- avatar -> ascii ---------------------------------------------------
    av = pathlib.Path(args.cache).with_name("avatar.png")
    if not args.offline:
        fetch_avatar(args.user, av)

    def top_of(key: str) -> str:
        xs = data.get(key) or []
        return xs[0]["name"] if xs else "—"

    env = {
        "editor": top_of("editors").lower(),
        "os": top_of("operating_systems").lower(),
        "daily": data.get("human_readable_daily_average") or "—",
        "range": d["range"] or "",
    }
    projs = data.get("projects") or []
    ptot = sum(p["total_seconds"] for p in projs) or 1.0
    pmax = max((p["total_seconds"] for p in projs), default=1.0) or 1.0
    projects = [(p["name"], p["total_seconds"] / pmax,
                 f'{p["total_seconds"] / ptot * 100:.1f}%')
                for p in sorted(projs, key=lambda p: -p["total_seconds"])[:4]]

    for theme in ("dark", "light"):
        inv = {"auto": theme == "light", "yes": True,
               "no": False}[args.portrait_invert]
        art = ascii_from_image(av, 40, 24, invert=inv)
        if art:
            (out / f"profile-{theme}.svg").write_text(render_profile(
                theme, args.user, art, gh, env, projects))
        (out / f"header-{theme}.svg").write_text(render_header(
            theme, "dhruvan",
            "compilers, graphics, ml infrastructure, and the tools around them",
            STRIP))
        (out / f"deck-{theme}.svg").write_text(render_deck(
            theme, DECK, "detail and links in the pinned repositories"))
        (out / f"waka-{theme}.svg").write_text(render_waka(theme, d))
        (out / f"pipeline-{theme}.svg").write_text(render_pipeline(
            theme, STAGES,
            "stage 1 compiles its own source through stage 0"))

    print(f"wrote {len(list(out.glob('*.svg')))} cards to {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
