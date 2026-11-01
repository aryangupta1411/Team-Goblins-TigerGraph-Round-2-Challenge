# The whole app as a container, for Railway or any other host: the Streamlit analyst dashboard (ui/) and everything a
# live agent run needs (agent/, engine/, the MCP launcher in mcp/, rag/ with the bge-large embedding model baked in,
# the checked-in facts snapshot data/hhgoa_engine.duckdb and the validator's data/ids.duckdb). Railway builds this file
# automatically; see "Deploy on Railway" in README.md.
#
# It still starts in mock mode and read-only (RUN_MODE=mock, DEPLOY_READONLY=1): it replays the canonical runs'
# traces (runs/${CANONICAL_RUN}, default hhf-final-3, plus the superseded hhf-final; runs/live-final-2 for Round 1) and touches no graph and no model.
# Live runs need Railway variables: RUN_MODE=live, DEPLOY_READONLY=0, LLM_BACKEND=gemini, the secrets TG_HOST,
# TG_SECRET and GEMINI_API_KEY, an approver directory (APPROVER_DIRECTORY_JSON) because RUNS_NEED_APPROVER=1 lets only
# a signed-in approver start a run, and AGENT_CASE_NAMESPACE so the deployment's write-backs (AC-<ns>-<case>) never
# overwrite the submitted run's AC-<case>. RUNS_DIR and APPROVALS_DB on a volume keep runs and decisions across
# redeploys; the canonical runs are copied onto it at start.
FROM python:3.12-slim

# The Final Round run promoted into cases/. Changing it also needs its !runs/<name>/ line in .dockerignore and its
# watch pattern in railway.json.
ARG CANONICAL_RUN=hhf-final-3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# torch from the PyTorch CPU index first (the PyPI Linux wheel pulls ~3 GB of CUDA libraries), then the rest of the
# pinned set from uv.lock (requirements-live.txt), wheels only; `pip check` fails the build if the set is not closed.
ARG TORCH_VERSION=2.14.0
RUN pip install --only-binary=:all: --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"
COPY requirements-live.txt ./
RUN pip install --only-binary=:all: -r requirements-live.txt && pip check

# The embedding model (rag/embed.py, EMBED_BACKEND=local), downloaded at build time so a run never waits on it. Only
# the safetensors weights and the sentence-transformers config: no ONNX/OpenVINO copies, and no torch at build time.
ENV HF_HOME=/opt/hf
RUN python -c "from huggingface_hub import snapshot_download as d; d('BAAI/bge-large-en-v1.5', ignore_patterns=['onnx/*', 'openvino/*', '*.onnx', '*.bin', '*.h5', '*.ot', '*.msgpack'])" \
    && chmod -R a+rX /opt/hf

# .dockerignore keeps everything else out of the build context (.env, data/raw/, the work DBs, tests/, docs/).
COPY .streamlit/config.toml .streamlit/
COPY agent/ agent/
COPY engine/ engine/
COPY etl/ etl/
COPY graph/ graph/
COPY mcp/ mcp/
COPY ops/ ops/
COPY policy/ policy/
COPY rag/ rag/
COPY schema_engine/ schema_engine/
COPY contracts/ contracts/
COPY data/ data/
COPY cases/ cases/
COPY runs/${CANONICAL_RUN}/ runs/${CANONICAL_RUN}/
COPY runs/hhf-final/ runs/hhf-final/
COPY runs/live-final-2/ runs/live-final-2/
COPY ui/ ui/

# runs/ is where agent.bench writes when RUNS_DIR is not a volume
RUN useradd --create-home --uid 10001 app && chown -R app runs
USER app

# Railway variables override any of these at runtime (see the header). HF_HUB_OFFLINE: the model is in the image.
ENV RUN_MODE=mock \
    DEPLOY_READONLY=1 \
    RUNS_NEED_APPROVER=1 \
    APPROVALS_DB=/tmp/approvals.sqlite \
    HF_HUB_OFFLINE=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=type

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT', '8501'), timeout=4)"

# Railway injects PORT at runtime; 8501 is the local default. When RUNS_DIR points outside the image (a volume), the
# canonical runs are copied there first without overwriting anything already on it. `exec` makes streamlit PID 1 so
# it gets SIGTERM. Run from /app so Streamlit picks up .streamlit/config.toml (the theme).
CMD ["sh", "-c", "if [ -n \"$RUNS_DIR\" ] && [ \"$RUNS_DIR\" != /app/runs ]; then mkdir -p \"$RUNS_DIR\" && cp -rn /app/runs/. \"$RUNS_DIR\"/; fi; exec streamlit run ui/app.py --server.address=0.0.0.0 --server.port=${PORT:-8501}"]
