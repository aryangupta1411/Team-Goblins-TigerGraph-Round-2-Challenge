---
name: HHGOA Fraud Investigator
description: An analyst console that reads like a regulator's typology case study. Petrol shell, white work surfaces, colour only as state.
colors:
  shell: "#0f3b57"
  shell-ink: "#ffffff"
  strip: "#e9f0f4"
  ground: "#ffffff"
  panel: "#f4f7f9"
  ink: "#14212b"
  ink-2: "#2c3d4b"
  muted: "#55687a"
  rule: "#dde5eb"
  rule-strong: "#c3d0da"
  widget-border: "#d5dee5"
  petrol: "#0f3b57"
  link: "#1f5a80"
  focus: "#1f5a80"
  chip: "#eef2f5"
  highlight: "#e6f0f6"
  gate: "#174a6a"
  gate-bg: "#e3edf4"
  gate-line: "#9fbdd2"
  red: "#c8102e"
  red-ink: "#9c0d24"
  red-bg: "#fbe9ec"
  red-line: "#e8a1ad"
  ochre: "#b7791f"
  ochre-ink: "#8a5a12"
  ochre-bg: "#fbf1e0"
  ochre-line: "#e3c48f"
  green: "#1d7a4e"
  green-ink: "#16603d"
  green-bg: "#e6f3ec"
  green-line: "#9ccfb4"
  quarantine: "#c2410c"
  quarantine-bg: "rgba(194, 65, 12, 0.035)"
  dark-ground: "#0c1a23"
  dark-strip: "#112634"
  dark-panel: "#10222e"
  dark-ink: "#e3eaef"
  dark-ink-2: "#c4d1db"
  dark-muted: "#93a7b6"
  dark-rule: "#1f3445"
  dark-rule-strong: "#2c4659"
  dark-petrol: "#8cc3e0"
  dark-button: "#2a7aa5"
  dark-chip: "#1b2f3c"
  dark-highlight: "#15324a"
  dark-gate: "#b9dcef"
  dark-gate-bg: "#15324a"
  dark-gate-line: "#2f5f80"
  dark-red: "#f2606f"
  dark-red-ink: "#ff9eaa"
  dark-red-bg: "#3a141b"
  dark-red-line: "#7c2a36"
  dark-ochre: "#e2a94f"
  dark-ochre-ink: "#f3cf8c"
  dark-ochre-bg: "#3a2a10"
  dark-ochre-line: "#7a5a26"
  dark-green: "#4fbf86"
  dark-green-ink: "#9be0bd"
  dark-green-bg: "#0f2c1f"
  dark-green-line: "#2c6b4b"
  dark-quarantine: "#f0874a"
typography:
  display:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "2.6rem"
    fontWeight: 650
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: "tnum"
  headline:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "1.65rem"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  figure:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "1.55rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
    fontFeature: "tnum"
  title:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.005em"
  lead:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "1.02rem"
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "0.9rem"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 500
    lineHeight: 1.3
  mono:
    fontFamily: "Roboto Mono, ui-monospace, monospace"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.35
    fontFeature: "tnum, liga 0, calt 0"
rounded:
  tag: "3px"
  sm: "4px"
  full: "50%"
spacing:
  xs: "0.3rem"
  sm: "0.55rem"
  md: "0.9rem"
  lg: "1.4rem"
  step-gap: "18px"
  band: "56px"
  gutter-narrow: "20px"
  container: "1480px"
components:
  shell-band:
    backgroundColor: "{colors.shell}"
    textColor: "{colors.shell-ink}"
    height: "{spacing.band}"
  context-strip:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.muted}"
    padding: "8px 0"
  button-primary:
    backgroundColor: "{colors.shell}"
    textColor: "{colors.shell-ink}"
    rounded: "{rounded.sm}"
  tab-list:
    backgroundColor: "{colors.strip}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
  tab-active:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
  section-head:
    textColor: "{colors.ink}"
    typography: "{typography.title}"
  figure-strip:
    backgroundColor: "{colors.ground}"
    rounded: "{rounded.sm}"
    padding: "0.9rem 0"
  tag-neutral:
    backgroundColor: "{colors.chip}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "0.05rem 0.4rem"
  tag-gate:
    backgroundColor: "{colors.gate-bg}"
    textColor: "{colors.gate}"
    rounded: "{rounded.tag}"
    padding: "0.05rem 0.4rem"
  tag-red:
    backgroundColor: "{colors.red-bg}"
    textColor: "{colors.red-ink}"
    rounded: "{rounded.tag}"
    padding: "0.05rem 0.4rem"
  tag-ochre:
    backgroundColor: "{colors.ochre-bg}"
    textColor: "{colors.ochre-ink}"
    rounded: "{rounded.tag}"
    padding: "0.05rem 0.4rem"
  tag-green:
    backgroundColor: "{colors.green-bg}"
    textColor: "{colors.green-ink}"
    rounded: "{rounded.tag}"
    padding: "0.05rem 0.4rem"
  scheme:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.sm}"
    padding: "0.7rem"
  scheme-step:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.6rem 0.6rem 0.55rem"
  scheme-step-number:
    backgroundColor: "{colors.petrol}"
    textColor: "{colors.ground}"
    rounded: "{rounded.full}"
    size: "1.2rem"
  evidence-key:
    textColor: "{colors.red-ink}"
    rounded: "{rounded.tag}"
    padding: "0 0.3rem"
  evidence-key-hover:
    backgroundColor: "{colors.red-bg}"
    textColor: "{colors.red-ink}"
  decision-figure:
    textColor: "{colors.ink}"
    typography: "{typography.display}"
  why-note:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.55rem 0.7rem"
  untrusted-frame:
    backgroundColor: "{colors.quarantine-bg}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.55rem 0.75rem"
  table-header:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
---

# Design System: HHGOA Fraud Investigator

## Overview

**Creative North Star: "The Typology Case Study"**

Every decision reads like a page from the FATF, Egmont and FinCEN typology reports the agent grounds itself on: the scheme drawn as numbered steps keyed to its red-flag indicators, with the decision and its human gates beside it. An institutional petrol owns the shell as a solid 56 px band. Under it, a pale petrol context strip carries source, case, state, files-only or live mode and provenance. Below that the work surfaces stay white, split by 1 px rules, and colour on them always means a state.

The console is dense, as an Operate surface should be, and calm. Hierarchy comes from scale on one baseline, not from boxes added for emphasis. Figures are type in ruled strips, never tiles. Each screen has one dominant region that answers its question on the first screen at 1440×900. Light and dark are both designed: the dark theme puts a petrol-black ground under the same petrol band, and every component token is emitted as a `light-dark()` pair on `.stApp`, so switching the theme repaints without a rerun.

The world rejects the category's two defaults: KPI tiles over a data grid buried in chips, and the dark SOC dashboard.

**Key Characteristics:**
- A petrol band (56 px) and a pale petrol context strip that bleeds to both window edges.
- White work surfaces, 1 px rules, 4 px corners, no resting shadows.
- Colour is a state: red, ochre, green, petrol gate, orange data warning. Grey is neutral.
- Public Sans for the interface, Roboto Mono for ids and query refs, tabular figures everywhere.
- One 1.5 px line-glyph legend on a 16 px grid serves the scheme, the case board and the graph legend.
- The signature: the case drawn as a numbered typology scheme whose evidence keys light their indicator rows.

## Colors

The palette is institutional and quiet: one petrol for the shell and the human gates, four state hues used only where a state exists, and a cool grey family for everything else.

### Primary
- **Typology Petrol** (`shell`, `petrol`): the top band in both themes, Streamlit's primary button, the section numbers, the scheme's step numbers and arrows, the ramp's engine and model marks, and the 2 px rule on top of the open tab. In dark mode the band stays the same petrol, while petrol used as text or line on the ground lifts to `dark-petrol` and the button fill to `dark-button`, which still carries white labels at 4.5:1.
- **Gate Petrol** (`gate`, `gate-bg`, `gate-line`): the L1 team lead and L2 fraud manager gates. It is used for gate tags with the gate glyph, the rule keys on the scheme's decision step, and the model's ±0.10 window on the ramp.

### Secondary
- **Red Flag** (`red`, `red-ink`, `red-bg`, `red-line`): red-flag indicators, fraud verdicts, hard actions (`BLOCK_CARD`, `BLOCK_ALL_CARDS`, `FILE_REPORT`) and failed checks. `red-ink` is the text colour; `red` is reserved for dots, marks and glyph strokes.
- **Awaiting Ochre** (`ochre`, `ochre-ink`, `ochre-bg`, `ochre-line`): uncertain verdicts and anything waiting on a human, such as a pending approval, the owner box of an open case, or flags on an indicator.
- **Executed Green** (`green`, `green-ink`, `green-bg`, `green-line`): legitimate verdicts, executed actions and passed checks.

### Tertiary
- **Data-Warning Orange** (`quarantine`, `quarantine-bg`): data and input warnings only. It is the dashed frame and faint warm wash around untrusted text, and the defence-code badges inside that frame. Orange never marks a verdict.

### Neutral
- **Paper White** (`ground`): the work surface and the page ground. Tables and dataframe headers sit on it with no header band.
- **Context Strip** (`strip`): the band under the shell, the tab list, and the hover state of dropdown items.
- **Panel Tint** (`panel`): the scheme's ground and the "why" note. It is the only tonal step used for depth.
- **Ink** (`ink`, `ink-2`): primary and secondary text.
- **Muted Slate** (`muted`): captions, table headers, section notes, the scope line and ramp ticks.
- **Hairline** (`rule`, `rule-strong`): `rule` sits under every row and section head and between strip columns; `rule-strong` sits under table headers and frames scheme steps and the open tab. Streamlit's own widgets and containers draw `widget-border`.
- **Chip and Highlight** (`chip`, `highlight`): the fill of a neutral tag and the ramp track; the row lit by an evidence key, a `:target` row and a hovered tab.

### Named Rules
**The Colour-Is-State Rule.** On a work surface a hue always means a state: red for red flags, fraud and hard actions; ochre for uncertain and awaiting a human; green for legitimate, executed and passed; petrol for the L1/L2 gates; orange for a data or input warning. Grey is neutral. Nothing is coloured for decoration, and the Streamlit badge names map one to one (`red`, `yellow`, `green`, `blue`, `orange`, `gray`).

**The Petrol-Owns-the-Shell Rule.** The band is petrol in both themes. On the work surface petrol appears only where it marks structure (section numbers, scheme steps and arrows, ramp marks) or a human gate.

**The Tokens-Only Rule.** Component and page CSS reads colour only from `var(--tc-*)`, which `ui/design.py` emits as `light-dark()` pairs. A raw hex in a stylesheet breaks the dark theme.

## Typography

**Display Font:** Public Sans (with system-ui, sans-serif)
**Body Font:** Public Sans (with system-ui, sans-serif)
**Label/Mono Font:** Roboto Mono (with ui-monospace, monospace)

**Character:** These are the US federal design system's faces, and FinCEN is a Treasury bureau. The pairing reads as a published report rather than a product dashboard. Every figure is tabular. Code and ids turn off ligatures so `0x`, `->` and ids read exactly as stored.

Sizes are rem at Streamlit's 15 px base (`baseFontSize = 15`).

### Hierarchy
- **Display** (650, 2.6rem, line-height 1, -0.02em, tabular): p(fraud) on the Case file's decision panel. It is the largest figure on that screen.
- **Headline** (650, 1.65rem, 1.2, -0.015em): the page's one h1 from `design.page_head`. The Case file's toolbar h1 is 1.75rem.
- **Figure** (600, 1.55rem, 1.2, -0.01em, tabular): headline numbers in figure strips. Labels above them are 0.82rem, 500, muted.
- **Title** (650, 1.05rem, -0.005em): section heads, after a petrol number at 700 when the order carries meaning. The case verdict line also sits at 1.05rem, 600.
- **Lead** (400, 1.02rem, 1.5, max 78ch): the page's answer sentence in ink. Key facts are `<b>` at 650 and state words take the state ink.
- **Body** (400, 0.9rem, 1.45): component text. Table cells run from 0.84 to 0.86rem.
- **Label** (500, 0.8rem, sentence case): table headers, tags, evidence keys, notes and provenance lines. It is the smallest size in the system, except the ramp's 12 px ticks.
- **Mono** (400 to 500, 0.8rem): transaction, card and device ids, query refs, action names and the untrusted text block.

### Named Rules
**The Tabular-Truth Rule.** Every number uses tabular figures, and every id, query ref and action code is set in Roboto Mono. A missing value prints as a grey dash, and a real zero prints as 0.

**The Sentence-Case Rule.** Headings, table headers, tabs and tags are sentence case at weight 500 to 650. No uppercase, no letter-spaced labels, and nothing above the h1.

## Layout

The shell is a fixed 56 px petrol band carrying the HHGOA mark and white top navigation. A 3 px white underline marks the current page. The context strip sits flush under the band and bleeds to both window edges through a full-width pseudo-element with one rule under it. The content column is capped at 1480 px. Under 1000 px wide the page keeps 20 px side gutters. Focus and anchors scroll to 120 to 132 px below the top, so they are never hidden under the band and strip.

Each screen has one dominant region that answers the page's question, and its first rows or figures are visible at 1440×900 without scrolling. The Case file's first view splits two thirds and one third. The left two thirds hold "1 Scheme" and "2 Red-flag indicators"; the right third holds "3 Decision" and "4 Actions". The tab bar shows at the bottom edge. Several headline numbers form one bordered strip split into columns by 1 px rules, with a 1.1rem inset on each column.

The rhythm is compact: 0.3rem inside lines, 0.55rem between a head's parts, 0.9rem between blocks and 1.4rem between the case brief's regions. Scheme steps sit 18 px apart. Under 1100 px the scheme stacks into one column, and its arrows turn downward.

**The One-Dominant-Region Rule.** One region per screen carries the answer. Everything else follows it in the order an investigator reads.

## Elevation & Depth

The system is flat at rest. Depth is conveyed by one tonal step: the panel tint under the scheme and the "why" note. Lines do the rest: 1 px rules, a stronger rule under headers, a dashed frame for quarantined text, and a verdict-coloured 1 px frame on a decision card. A single shadow exists, and only on something floating.

### Shadow Vocabulary
- **Overlay** (`box-shadow: 0 8px 24px rgba(8, 26, 38, .18)`): the top-navigation dropdown popover, which floats over the page. It is used nowhere else.

### Named Rules
**The Flat-At-Rest Rule.** No surface casts a shadow at rest. A region earns separation from a rule or the panel tint, never from elevation. No gradients, glass or blur.

## Shapes

Corners are a single small step. Containers, expanders, dataframes, alerts, buttons, the scheme and its steps take 4 px. Tags, evidence keys and rule keys take 3 px. Circles appear only for verdict dots (0.6rem), scheme step numbers (1.2rem) and the ramp's engine and model marks. Lines are 1 px everywhere. The only heavier lines are the 1.5 px glyph strokes and scheme arrows, the 2 px petrol top rule on the open tab, the 3 px underline on the current nav link and 2 px focus outlines. Scheme arrowheads are clipped triangles. Untrusted text alone gets a dashed frame, and quiet scheme steps (no evidence) get a dashed border.

The glyph legend is one set of 1.5 px line icons on a 16 px grid with round caps and joins, drawn in `currentColor`: alert, card, transaction, device, wave, customer, closed_case, agent_case, decision, email, region, check, cross, clock, gate, shield, dash.

**The Four-Pixel Rule.** Containers take 4 px corners, tags and keys take 3 px, and only dots and markers are round. No larger radii.

**The Hairline Rule.** Structure is drawn with 1 px rules. A heavier line always marks a state: the current page, the open tab, focus.

## Components

### Shell band and context strip
The band holds the petrol mark, the white nav at weight 500 and opacity 0.74, and the current page at full opacity and weight 600 with a 3 px white underline. Hovering or opening a nav section raises it to full opacity (0.15s ease-out). Dropdown items are 36 px rows with 3 px corners and a strip-coloured hover. The context strip uses 0.82rem muted text, values in ink at 600 and rule-grey separators. Its selects sit on the white ground.

### Buttons
- **Shape:** gently squared (4px), from Streamlit's own button with `buttonRadius = "4px"`.
- **Primary:** petrol fill with white label. In dark mode the fill is `dark-button`.
- **Hover / Focus:** the hover is Streamlit's own. Focus is a 2 px `focus` outline at a 2 px offset on every link, button, summary and tab.
- **Tertiary and page links:** petrol text with no fill. A disabled control gives its reason in its tooltip; the live-run button lists what it needs.

### Tags
- **Style:** flat, 3 px corners, 0.8rem at 500, padded 0.05rem by 0.4rem, with an optional leading glyph. There are five kinds: neutral (chip), gate (petrol, with the gate glyph), red, ochre and green.
- **Route tag:** "auto" is neutral, "L1 · team lead" and "L2 · fraud manager" are gate tags.

### Section heads
A flex row on one baseline: an optional petrol number at 700, the h2 title at 1.05rem/650, a muted note and a right-aligned aside, with a 1 px rule under it. Numbers appear only where the order carries meaning, such as a pipeline or a story.

### Figure strips
Headline numbers are rendered by `common.measured` inside a bordered container keyed `strip-<name>`. The strip drops each metric's own frame and padding, and splits the columns with 1 px rules. The figure is 1.55rem/600 and the label is 0.82rem/500 muted. The provenance (source file, as-of time and method) lives in the tooltip.

### Checks line
One wrapping line at 0.82rem after a bold lead: a green check glyph for a pass, a red cross with red ink for a failure, plain muted text otherwise. An optional link sits at the right end.

### Tables
`tc-table` holds short fixed lists. Its header is muted, 0.8rem/500, in sentence case over a strong rule. Each row has one 1 px rule, numbers align right, and ids are set in mono. Quiet rows go muted, and a sub-line sits under a cell. Large or sortable data stays in `st.dataframe`, where the theme removes the header band and draws the same rules.

### Untrusted text
`common.untrusted_text` builds a container keyed `untrusted-<key>` with a 1 px dashed orange frame and a 3.5 % orange wash. Inside sit the caption, the text verbatim in a mono code block, orange defence-code badges or a green "no injection markers" badge, and the sanitised text in an expander. The text is never rendered as markup and never followed.

### Decision panel and ramp
p(fraud) sits at display size beside a muted label naming the verdict. Under it is the fixed 0–1 ramp: a 6 px chip track, the model's ±0.10 window in gate tint around the engine's value, the engine as a 12 px ring in petrol, the model as an 8 px petrol dot and the final value as a 4×20 px bar in the verdict colour. Ticks at 0, 0.25, 0.5, 0.75 and 1 are 12 px muted. A definition list gives the decision path. The "why" note sits on panel tint. The actions table shows action, route tag and where it stands: a green check for done, ochre for waiting and muted for off, with hard actions in red ink. An ochre-framed owner box (green once resolved) names who must decide.

### Tabs
The tab list is a strip band with a hairline frame and 4 px top corners. Closed tabs are muted 0.95rem/600 labels that take the highlight on hover. The open tab takes the white ground, side rules and a 2 px petrol top rule, and sits over the list's bottom rule so it joins its panel. The focus ring is drawn inside the tab.

### The scheme (signature)
The case drawn as a typology scheme, built in `ui/case_brief.py` only from the case's own files. It is a grid of up to five steps on the panel tint in a 4 px ruled frame: alert, card, signal, pattern, decision. Each step is a white box with a 1 px strong rule and 4 px corners. Its top line holds a petrol numbered circle, a 0.82rem/650 title and the step's glyph at the right. Its lines below are 0.8rem: one large line at 1.05rem/650, flag lines in red ink and ids in mono. Petrol 1.5 px arrows with clipped heads join the steps. The decision step takes its verdict's 1 px line colour, and a step with no evidence is dashed. Evidence keys (E1, E2 …) sit at the foot of each step. Each is a 3 px outlined key in the direction of its claim's family: red for fraud, green for legitimate, muted for neutral. Gate-petrol rule keys (§, R1–R10) sit on the decision step. Hovering a key lights its row in the red-flag indicators table below (key, family and direction, indicator with its query ref in mono, ids, checked time). Clicking a key jumps to that row and holds it highlighted as `:target`. The untrusted alert text sits verbatim beneath the scheme in its quarantine frame.

## Do's and Don'ts

### Do:
- **Do** render every headline number through `common.measured(label, value, source=, as_of=, ...)`. To show several, put them in `st.container(border=True, key="strip-<name>")` with `st.columns(n)` and pass `container=cols[i]`.
- **Do** escape every value that reaches HTML with `design.esc()`, and render built markup with `design.html_block()`. Pass Markdown text from data through `common.md()`.
- **Do** show hostile or untrusted text (triggers, payloads, model output) only through `common.untrusted_text(...)` or `st.code`.
- **Do** put page CSS in `ui/assets/css/<page>.css`, which `design.inject()` loads after `console.css`. Prefix its classes per page (`.ov-`, `.ap-`, `.gr-`, `.dt-`, `.sc-`, `.rn-`, `.dq-`, `.sy-`, `.rc-`) and use only `var(--tc-*)` colours.
- **Do** open every page with `common.page_header(title, subtitle, eyebrow=scope)`, which puts the h1 first with the scope and data sentence under it. Follow it with the page's answer as one `tc-lead` sentence.
- **Do** build with the design API: `design.section`, `design.tag`, `design.route_tag`, `design.checks`, `design.ramp`, `design.glyph`.
- **Do** draw p(fraud) on the fixed 0–1 ramp with the model's ±0.10 window wherever it appears.
- **Do** make files-only versus live graph an explicit, visible state in the context strip.
- **Do** print a grey dash for a missing value and 0 for a real zero, and keep "not measured here" where a file is missing.

### Don't:
- **Don't** write a raw hex colour in page or component CSS. The `--tc-*` tokens carry both themes.
- **Don't** add resting shadows, gradients, glass or blur.
- **Don't** put a label, eyebrow or kicker above a heading, or use tiny uppercase letter-spaced labels.
- **Don't** give a card, list item or callout a coloured side border wider than 1 px.
- **Don't** lay out KPIs as tiles or rows of identical icon-and-heading cards. Figures are type in a ruled strip.
- **Don't** nest bordered cards. The scheme's step boxes on its panel tint are the diagram's nodes, the one sanctioned nesting.
- **Don't** use emoji or unicode pictograms as icons. Draw entities only from the glyph legend.
- **Don't** colour anything that is not a state, or use orange for anything but a data or input warning.
- **Don't** write captions that describe the interface ("click to…", "this page shows…"). Copy states the data, in the product's words.
- **Don't** let a label overclaim: pending never reads as filed or contained, mock mode never claims the graph was queried, and simulated replies stay marked.
