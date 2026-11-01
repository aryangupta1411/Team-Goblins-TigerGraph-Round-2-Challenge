"""The console's design layer: theme tokens, the stylesheet, the line-glyph legend and small HTML pieces.

The world is the typology case study (the FATF / Egmont / FinCEN typology reports the agent grounds itself on): a
petrol shell band, white work surfaces split by 1 px rules, numbered sections and one 1.5 px line-glyph legend.
ui/assets/console.css holds every rule; this module injects the --tc-* tokens for the active theme ahead of it, because
Streamlit exposes no theme variables to CSS. Colours mirror .streamlit/config.toml.

Every value that reaches HTML here goes through esc(): case data, model output and trigger text are never markup.
"""
from __future__ import annotations

import html
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import streamlit as st

CSS_PATH = Path(__file__).parent / "assets" / "console.css"

TOKENS = {
    "light": {
        "shell": "#0f3b57", "shell-ink": "#ffffff", "strip": "#e9f0f4", "ground": "#ffffff", "panel": "#f4f7f9",
        "ink": "#14212b", "ink-2": "#2c3d4b", "muted": "#55687a", "rule": "#dde5eb", "rule-strong": "#c3d0da",
        "petrol": "#0f3b57", "link": "#1f5a80", "chip": "#eef2f5",
        "gate": "#174a6a", "gate-bg": "#e3edf4", "gate-line": "#9fbdd2",
        "red": "#c8102e", "red-ink": "#9c0d24", "red-bg": "#fbe9ec", "red-line": "#e8a1ad",
        "ochre": "#b7791f", "ochre-ink": "#8a5a12", "ochre-bg": "#fbf1e0", "ochre-line": "#e3c48f",
        "green": "#1d7a4e", "green-ink": "#16603d", "green-bg": "#e6f3ec", "green-line": "#9ccfb4",
        "quarantine": "#c2410c", "quarantine-bg": "rgba(194, 65, 12, .035)", "highlight": "#e6f0f6",
        "select": "rgba(31, 90, 128, .22)", "focus": "#1f5a80",
    },
    "dark": {
        "shell": "#0f3b57", "shell-ink": "#f2f7fa", "strip": "#112634", "ground": "#0c1a23", "panel": "#10222e",
        "ink": "#e3eaef", "ink-2": "#c4d1db", "muted": "#93a7b6", "rule": "#1f3445", "rule-strong": "#2c4659",
        "petrol": "#8cc3e0", "link": "#8cc3e0", "chip": "#1b2f3c",
        "gate": "#b9dcef", "gate-bg": "#15324a", "gate-line": "#2f5f80",
        "red": "#f2606f", "red-ink": "#ff9eaa", "red-bg": "#3a141b", "red-line": "#7c2a36",
        "ochre": "#e2a94f", "ochre-ink": "#f3cf8c", "ochre-bg": "#3a2a10", "ochre-line": "#7a5a26",
        "green": "#4fbf86", "green-ink": "#9be0bd", "green-bg": "#0f2c1f", "green-line": "#2c6b4b",
        "quarantine": "#f0874a", "quarantine-bg": "rgba(240, 135, 74, .05)", "highlight": "#15324a",
        "select": "rgba(140, 195, 224, .3)", "focus": "#8cc3e0",
    },
}

# One line-glyph legend for the whole console (16 px grid, 1.5 px stroke, round joins). The scheme, the case board and
# the graph legend draw their entities from this set and nothing else.
_GLYPHS = {
    "alert": '<path d="M3.5 14.5V2"/><path d="M3.5 2.5h8.5l-2 3 2 3H3.5"/>',
    "card": '<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="M1.5 6.5h13M4 10h3"/>',
    "transaction": '<path d="M2 5.5h11M10 2.5l3 3-3 3M14 10.5H3M6 7.5l-3 3 3 3"/>',
    "device": '<rect x="4.5" y="1.5" width="7" height="13" rx="1.5"/><path d="M7 12.5h2"/>',
    "wave": '<circle cx="3" cy="8" r="1.6"/><circle cx="8" cy="8" r="1.6"/><circle cx="13" cy="4" r="1.6"/><circle cx="13" cy="12" r="1.6"/>'
            '<path d="M4.6 8h1.8M9.4 7.2l2.2-2.4M9.4 8.8l2.2 2.4"/>',
    "customer": '<circle cx="8" cy="5.5" r="2.5"/><path d="M3 14c.7-2.8 2.7-4.3 5-4.3s4.3 1.5 5 4.3"/>',
    "closed_case": '<path d="M1.5 4.5V13h13V6h-6.5L6.5 4.5z"/>',
    "agent_case": '<path d="M1.5 4.5V13h13V6h-6.5L6.5 4.5z"/><path d="M5.5 9.6l1.7 1.6 3.3-3.3"/>',
    "decision": '<path d="M8 2.5v11M5 13.5h6M3 5h10"/><path d="M3 5l-1.8 4h3.6zM13 5l-1.8 4h3.6z"/>',
    "email": '<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="M2 4.5l6 4.5 6-4.5"/>',
    "region": '<path d="M8 14.5s4.5-4.2 4.5-7.6a4.5 4.5 0 0 0-9 0c0 3.4 4.5 7.6 4.5 7.6z"/><circle cx="8" cy="6.8" r="1.6"/>',
    "check": '<path d="M3 8.5l3 3 7-7"/>',
    "cross": '<path d="M4 4l8 8M12 4l-8 8"/>',
    "clock": '<circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/>',
    "gate": '<rect x="3.5" y="7" width="9" height="7" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
    "shield": '<path d="M8 1.5l5.5 2v4.2c0 3.3-2.4 5.9-5.5 6.8-3.1-.9-5.5-3.5-5.5-6.8V3.5z"/>',
    "dash": '<path d="M4 8h8"/>',
}


def esc(value: object) -> str:
    """Any value as HTML text: None is empty, everything else is escaped (quotes included, so it is safe in attributes)."""
    return "" if value is None else html.escape(str(value), quote=True)


def glyph(name: str, cls: str = "", title: str = "") -> str:
    """One glyph of the legend as a span painted by a CSS mask (glyph_css()): st.html sanitizes inline <svg> away, a
    classed span survives it. An unknown name draws nothing."""
    if name not in _GLYPHS:
        return ""
    label = f' role="img" aria-label="{esc(title)}"' if title else ' aria-hidden="true"'
    return f'<span class="tc-glyph g-{esc(name)} {esc(cls)}"{label}></span>'


def glyph_css() -> str:
    """One mask rule per glyph: the legend's paths as an SVG data URI, painted in currentColor."""
    from urllib.parse import quote

    rules = []
    for name, body in _GLYPHS.items():
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='black' stroke-width='1.5' "
               f"stroke-linecap='round' stroke-linejoin='round'>{body}</svg>").replace('"', "'")
        uri = "url(\"data:image/svg+xml;charset=utf-8," + quote(svg, safe="/:=' ") + "\")"
        rules.append(f".tc-glyph.g-{name}{{-webkit-mask-image:{uri};mask-image:{uri}}}")
    return "".join(rules)


def theme_type() -> str:
    """'light' or 'dark' from st.context.theme (light when the runtime does not say)."""
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001 - bare mode and older runtimes have no theme context
        return "light"


def tokens_css(theme: str | None = None) -> str:
    """The --tc-* custom properties. Without `theme`, each token is light-dark(light, dark), declared on .stApp: Streamlit
    sets color-scheme there for the active theme and the browser resolves light-dark() inside a custom property where it
    is declared, so a theme switch in the app menu repaints the components at once, with no rerun. :root carries the same
    declarations for anything rendered outside .stApp (it resolves light there). With `theme`, one fixed set."""
    if theme in TOKENS:
        return ":root,.stApp{" + "".join(f"--tc-{k}:{v};" for k, v in TOKENS[theme].items()) + "}"
    light, dark = TOKENS["light"], TOKENS["dark"]
    return ":root,.stApp{" + "".join(f"--tc-{k}:light-dark({light[k]},{dark[k]});" for k in light) + "}"


PAGE_CSS_DIR = Path(__file__).parent / "assets" / "css"
_CSS_CACHE: dict[str, Any] = {"sig": None, "text": ""}


def _css_files() -> list[Path]:
    """console.css first, then one stylesheet per page from ui/assets/css/ in name order (each page owns its file)."""
    pages = sorted(PAGE_CSS_DIR.glob("*.css")) if PAGE_CSS_DIR.is_dir() else []
    return [CSS_PATH, *pages]


def _stylesheet() -> str:
    """The console's CSS, re-read when any file changes (a missing file is skipped; none leaves the theme's defaults)."""
    files = []
    for path in _css_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path, stat.st_mtime_ns, stat.st_size))
    sig = tuple(files)
    if _CSS_CACHE["sig"] != sig:
        _CSS_CACHE.update(sig=sig, text="\n".join(p.read_text(encoding="utf-8") for p, _, _ in files))
    return _CSS_CACHE["text"]


def inject() -> None:
    """The tokens for the active theme, then the stylesheet. Called once per run by common.inject_css(). A dialog renders
    outside .stApp, where light-dark() would resolve light, so it gets the active theme's color-scheme."""
    dialog = f'[data-testid="stDialog"]{{color-scheme:{theme_type()}}}'
    st.markdown(f"<style>{tokens_css()}\n{dialog}\n{_stylesheet()}\n{glyph_css()}</style>", unsafe_allow_html=True)
    if not st.session_state.get("_tc_light_default"):
        st.session_state["_tc_light_default"] = True
        st.html(LIGHT_DEFAULT_JS, unsafe_allow_javascript=True)


# Open in light whatever the viewer's OS says. With both themes in config.toml Streamlit follows the OS and ignores
# theme.base, so on a browser's first visit this writes "Light" to the key the app menu's theme switcher uses and
# reloads once. The marker key makes it once per browser: a theme picked later in the menu (System or Dark) is kept.
LIGHT_DEFAULT_JS = """<script>
try {
  const ls = window.localStorage, key = "stActiveTheme-/-v2", mark = "tc-light-default-v1";
  if (!ls.getItem(mark)) {
    ls.setItem(mark, "1");
    if (ls.getItem(key) !== JSON.stringify("Light")) { ls.setItem(key, JSON.stringify("Light")); window.location.reload(); }
  }
} catch (e) {}
</script>"""


def html_block(markup: str) -> None:
    """Render a component's HTML (already escaped by its builder) in the page's own DOM, so the stylesheet applies."""
    st.html(markup)


# --------------------------------------------------------------------------- #
# Small pieces                                                                #
# --------------------------------------------------------------------------- #
def section(n: object, title: str, note: str = "", aside: str = "") -> str:
    """A numbered section head: the number in petrol, a sentence-case title, a grey note and a right-aligned aside.
    `aside` may carry markup the caller built with esc(); the other arguments are text."""
    num = f'<span class="tc-n">{esc(n)}</span>' if n not in (None, "") else ""
    parts = [num, f"<h2>{esc(title)}</h2>"]
    if note:
        parts.append(f'<span class="tc-sec-note">{esc(note)}</span>')
    if aside:
        parts.append(f'<span class="tc-sec-aside">{aside}</span>')
    return f'<div class="tc-sec">{"".join(parts)}</div>'


def tag(text: object, kind: str = "", icon: str = "") -> str:
    """A flat 3 px tag. kind: '' (neutral), 'gate' (petrol, the L1/L2 human gates), 'red', 'ochre' or 'green'."""
    return f'<span class="tc-tag {esc(kind)}">{glyph(icon) if icon else ""}{esc(text)}</span>'


def route_tag(route: str | None) -> str:
    route = str(route or "auto")
    labels = {"auto": "auto", "L1": "L1 · team lead", "L2": "L2 · fraud manager"}
    return tag(labels.get(route, route), "" if route == "auto" else "gate", "" if route == "auto" else "gate")


def checks(items: Iterable[tuple[bool | None, str]], lead: str = "", tail: str = "") -> str:
    """One line of checks: (True, text) is a green check, (False, text) a red cross, (None, text) plain text. `tail` is
    markup the caller built with esc(), set at the line's right end (e.g. a jump link)."""
    out = [f"<b>{esc(lead)}</b>"] if lead else []
    for ok, text in items:
        if ok is None:
            out.append(f'<span class="tc-check">{esc(text)}</span>')
        else:
            out.append(f'<span class="tc-check {"ok" if ok else "bad"}">{glyph("check" if ok else "cross")}{esc(text)}</span>')
    return f'<div class="tc tc-checks">{"".join(out)}{tail}</div>'


def _clip01(x: float | None) -> float | None:
    return None if x is None else max(0.0, min(1.0, float(x)))


def ramp(engine: float | None, model: float | None, final: float | None, verdict: str, window: float = 0.10) -> str:
    """The fixed 0-1 probability ramp: the engine's value with the model's +/-window around it, the model's value inside
    it and the final value as the verdict-coloured bar. Values that are missing are simply not drawn."""
    e, m, f = _clip01(engine), _clip01(model), _clip01(final)
    parts = ['<div class="track"></div>']
    if e is not None:
        lo, hi = max(0.0, e - window), min(1.0, e + window)
        parts.append(f'<div class="window" style="left:{lo * 100:.2f}%;width:{(hi - lo) * 100:.2f}%"></div>')
        parts.append(f'<div class="mark engine" style="left:{e * 100:.2f}%" title="P3 engine {e:.2f}"></div>')
    if m is not None:
        parts.append(f'<div class="mark model" style="left:{m * 100:.2f}%" title="P4 model {m:.2f}"></div>')
    if f is not None:
        parts.append(f'<div class="mark final {esc(verdict)}" style="left:{f * 100:.2f}%" title="final {f:.2f}"></div>')
    for t in (0, 0.25, 0.5, 0.75, 1):
        parts.append(f'<span class="tick" style="left:{t * 100:.0f}%">{t:g}</span>')
    label = "probability of fraud on a 0 to 1 scale" + (f", final {f:.2f}" if f is not None else "")
    return f'<div class="tc-ramp" role="img" aria-label="{esc(label)}">{"".join(parts)}</div>'


def page_head(title: str, scope: str = "", sentence: str = "") -> str:
    """The page's one h1, then the scope line and the data sentence: nothing sits above the title."""
    lines = "".join(f"<p>{esc(x)}</p>" for x in (scope, sentence) if x)
    return f'<div class="tc tc-head"><h1>{esc(title)}</h1>{lines}</div>'
