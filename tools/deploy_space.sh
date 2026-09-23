#!/usr/bin/env bash
# Assemble + push the Jaarvis voice Space from this repo (single source of truth).
# The Space repo is a BUILD ARTIFACT: server.py, static/, core/ and requirements
# are copied verbatim — never edited in the artifact. Soul/tool/prompt changes
# happen here and redeploy with this script.
#
# Usage: HF_TOKEN=hf_xxx HF_SPACE=username/jaarvis-live bash tools/deploy_space.sh
set -euo pipefail
: "${HF_TOKEN:?set HF_TOKEN}"
: "${HF_SPACE:?set HF_SPACE like username/jaarvis-live}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp "$ROOT/space/server.py" "$ROOT/space/requirements.txt" "$ROOT/space/README.md" "$WORK/"
cp -r "$ROOT/space/static" "$ROOT/core" "$WORK/"
mkdir -p "$WORK/memory"  # server-side store boots empty; memory shape identical
cat > "$WORK/Dockerfile" <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static/ ./static/
COPY core/ ./core/
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
EOF
cd "$WORK"
git init -q
git add -A
git commit -qm "Jaarvis voice Space (assembled from monorepo)"
git remote add space "https://user:${HF_TOKEN}@huggingface.co/spaces/${HF_SPACE}"
git push -f space "HEAD:refs/heads/main"
echo "SPACE LIVE: https://huggingface.co/spaces/${HF_SPACE}"
