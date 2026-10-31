"""Router: the app's entry point, with no page body of its own.

Run from the repo root:  uv run streamlit run ui/app.py   (or `make ui`)

st.navigation draws the top bar from common.nav_pages(): Overview, Case file, Graph and Approvals, then the Proof and
System dropdowns. common.context_bar() then renders the one bar every page shares (source and case pickers, provenance,
mode, identity), keeps ?run= / ?case= in step with the pickers and stores the resolved context, which each page reads
with common.page_setup("<key>") before pg.run() executes it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root on sys.path (streamlit adds ui/ only)

from ui import common  # noqa: E402

# no page_title: each st.Page title names the browser tab
st.set_page_config(page_icon=":material/verified_user:", layout="wide", initial_sidebar_state="collapsed")
common.inject_css()
st.logo(str(Path(__file__).parent / "assets" / "mark.svg"), size="large")
pg =st.navigation(common.nav_pages(), position="top")
common.context_bar(common.page_key_for(pg))
pg.run()
