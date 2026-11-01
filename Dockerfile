# The Streamlit analyst dashboard (ui/) as a container, for Railway or any other host. Railway builds
# this file automatically; see "Deploy on Railway" in README.md.
# Railway deploys this repo's main branch with the DOCKERFILE builder (railway.json).
#
# The image holds the UI, the answer files, the canonical runs' traces (runs/${CANONICAL_RUN}, default hhf-final, for
# the Final Round; runs/live-final-2 for Round 1), the engine's saved detector backtest and containment summary, and the
# measured proof snapshot (engine/out/proof_snapshot.json, from `make proof-snapshot`) that stands in for the engine
# sections the image cannot compute. Besides ui/ it ships two stdlib-only modules: agent/security.py (the intake
# sanitiser) and schema_engine/attestation.py (which it imports). It installs only what ui/ imports
# (requirements-ui.txt, pinned to uv.lock): no torch, no agent stack, no duckdb and no data/. It starts in mock mode
# and read-only: no agent runs, no graph writes.
FROM python:3.12-slim

# The Final Round run promoted into cases/. Changing it also needs its !runs/<name>/ line in .dockerignore and its
# watch pattern in railway.json.
ARG CANONICAL_RUN=hhf-final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Wheels only, exactly the pinned set; `pip check` fails the build if the list is not closed.
COPY requirements-ui.txt ./
RUN pip install --no-deps --only-binary=:all: -r requirements-ui.txt && pip check

# Only the files ui/ reads at runtime. .dockerignore keeps everything else out of the build context.
COPY .streamlit/config.toml .streamlit/
COPY ops/__init__.py ops/ensure_awake.py ops/
COPY rag/__init__.py rag/validate_sar.py rag/
COPY agent/__init__.py agent/security.py agent/
COPY schema_engine/__init__.py schema_engine/attestation.py schema_engine/
COPY cases/ cases/
COPY engine/out/detector_backtest.json engine/out/containment_summary.json engine/out/proof_snapshot.json engine/out/
COPY runs/${CANONICAL_RUN}/ runs/${CANONICAL_RUN}/
COPY runs/live-final-2/ runs/live-final-2/
COPY ui/ ui/

RUN useradd --create-home --uid 10001 app
USER app

# Railway variables override any of these at runtime. Live mode needs RUN_MODE=live plus TG_HOST and
# TG_SECRET; DEPLOY_READONLY=1 still keeps the agent subprocess and the graph writes switched off.
# APPROVALS_DB sits on the container's disk, which every redeploy wipes: attach a volume and point it
# there to keep decisions (README.md).
ENV RUN_MODE=mock \
    DEPLOY_READONLY=1 \
    APPROVALS_DB=/tmp/approvals.sqlite \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=type

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT', '8501'), timeout=4)"

# Railway injects PORT at runtime; 8501 is the local default. `exec` makes streamlit PID 1 so it gets SIGTERM.
# Run from /app so Streamlit picks up .streamlit/config.toml (the theme).
CMD ["sh", "-c", "exec streamlit run ui/app.py --server.address=0.0.0.0 --server.port=${PORT:-8501}"]
