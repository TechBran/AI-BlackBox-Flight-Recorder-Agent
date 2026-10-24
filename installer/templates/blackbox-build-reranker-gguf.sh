#!/usr/bin/env bash
# blackbox-build-reranker-gguf — build the on-box reranker's GGUF
# (Qwen3-Reranker-8B @ Q8_0) reproducibly, at pinned revisions.
#
# The llama-swap member rerank-qwen3-8b loads
# ${models-dir}/Qwen3-Reranker-8B-Q8_0.gguf, but nothing produced that file:
# it is self-converted (community reranker GGUFs drop cls.output.weight and
# score ~1e-28), DOWNLOAD_MANIFEST has no entry for it, and the runbook
# (LocalModels/reranker/CONVERT-QWEN3-RERANKER-GGUF.md) was a hand procedure
# whose llama.cpp tag was never recorded. A fresh box therefore shipped a member
# that could never start (audit O19-local-stack-10). This automates runbook
# steps 1-5:
#   1. clone llama.cpp at the tag in llamacpp-version (the release the
#      installer builds llama-server from) and check its convert script has the
#      Qwen3-Reranker fix (#16407);
#   2. a throwaway conversion venv from that tag's own requirements;
#   3. download the HF checkpoint at the revision in reranker-gguf-version;
#   4. convert to f16, build llama-quantize (CPU is enough), quantize to Q8_0;
#   5. check the reranker head (cls.output.weight) survived, compare against
#      the G2-validated reference sha256, move the file into place atomically
#      and write <gguf>.provenance.json beside it.
# Then run the G2 gate (eval/rerank_g2.py) before selecting the reranker.
#
# Run it as the box's user (it writes into ~/.blackbox/localstack/models; root
# would leave root-owned files there). Heavy: ~16 GB download, ~42 GB of
# scratch space, tens of minutes. Re-run safe: an existing GGUF is kept unless
# --force.
#
# Usage: bash installer/templates/blackbox-build-reranker-gguf.sh \
#            [--models-dir DIR] [--work-dir DIR] [--keep-work] [--force]
# Environment: REQUIRED_FREE_GB (default 42) for the scratch-space check.
#
# Exit codes:
#   0 — built (or already present without --force)
#   2 — bad arguments, run as root, unpinned pins file, or not enough disk
#   3 — the pinned llama.cpp cannot convert a Qwen3-Reranker, or the result
#       lacks the reranker head (a broken conversion: do not use it)
#   4 — a clone / download / venv / build / convert step failed
set -euo pipefail

log() { echo "[build-reranker-gguf] $*"; }
die() { local rc="$1"; shift; echo "[build-reranker-gguf] ERROR: $*" >&2; exit "$rc"; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE_DIR="$REPO_ROOT/installer/templates"
OUT_NAME="Qwen3-Reranker-8B-Q8_0.gguf"
MODELS_DIR="${HOME}/.blackbox/localstack/models"
WORK_DIR=""
KEEP_WORK=0
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --models-dir) MODELS_DIR="${2:-}"; shift 2 ;;
        --work-dir) WORK_DIR="${2:-}"; shift 2 ;;
        --keep-work) KEEP_WORK=1; shift ;;
        --force) FORCE=1; shift ;;
        *) die 2 "unknown argument: $1" ;;
    esac
done
[[ -n "$MODELS_DIR" ]] || die 2 "--models-dir needs a directory"
if [[ $EUID -eq 0 ]]; then
    die 2 "run this as the box's user, not root (it writes into the user's models dir)"
fi

pins() { grep -vE '^[[:space:]]*(#|$)' "$1" | tr -d '[:blank:]\r'; }
mapfile -t _LC < <(pins "$TEMPLATE_DIR/llamacpp-version")
mapfile -t _RR < <(pins "$TEMPLATE_DIR/reranker-gguf-version")
LC_VER="${_LC[0]:-}"
HF_REPO="${_RR[0]:-}"; HF_REV="${_RR[1]:-}"; REF_SHA="${_RR[2]:-}"
[[ "$LC_VER" =~ ^b[0-9]+$ ]] || die 2 "no llama.cpp tag pinned in llamacpp-version (got '$LC_VER')"
[[ -n "$HF_REPO" ]] || die 2 "no HF repo pinned in reranker-gguf-version"
[[ "$HF_REV" =~ ^[0-9a-f]{40}$ ]] || die 2 "HF revision in reranker-gguf-version is not a 40-hex commit (got '$HF_REV')"
[[ "$REF_SHA" =~ ^[0-9a-f]{64}$ ]] || die 2 "reference sha256 in reranker-gguf-version is not a 64-hex digest"

OUT="$MODELS_DIR/$OUT_NAME"
if [[ -s "$OUT" && "$FORCE" -eq 0 ]]; then
    log "$OUT already exists — nothing to do (--force rebuilds it)."
    exit 0
fi

mkdir -p "$MODELS_DIR"
# Scratch on the models filesystem by default, so the final move is atomic.
[[ -n "$WORK_DIR" ]] || WORK_DIR="$MODELS_DIR/.reranker-gguf-build"
mkdir -p "$WORK_DIR"
need_gb="${REQUIRED_FREE_GB:-42}"
free_kb="$(df -Pk "$WORK_DIR" | awk 'NR==2 {print $4}')"
if [[ ! "$free_kb" =~ ^[0-9]+$ ]] || (( free_kb < need_gb * 1024 * 1024 )); then
    die 2 "need ${need_gb} GB free under $WORK_DIR for the download + f16 + Q8_0 files (have $(( ${free_kb:-0} / 1024 / 1024 )) GB)"
fi
cleanup() { [[ "$KEEP_WORK" -eq 1 ]] || rm -rf "$WORK_DIR"; }
trap cleanup EXIT

LC_DIR="$WORK_DIR/llama.cpp"
HF_DIR="$WORK_DIR/hf"
VENV="$WORK_DIR/convert-venv"
F16="$WORK_DIR/Qwen3-Reranker-8B-f16.gguf"
PART="$WORK_DIR/$OUT_NAME.part"
PYBIN="$(command -v python3.12 || command -v python3)"

# ── 1. llama.cpp at the pinned tag, with the Qwen3-Reranker convert fix ──────
log "Cloning llama.cpp $LC_VER..."
rm -rf "$LC_DIR"
git clone --depth 1 --branch "$LC_VER" https://github.com/ggml-org/llama.cpp "$LC_DIR" \
    || die 4 "llama.cpp clone at $LC_VER failed"
CONVERT="$LC_DIR/convert_hf_to_gguf.py"
if [[ ! -f "$CONVERT" ]] || ! grep -q 'cls\.output\.weight' "$CONVERT" \
        || ! grep -qiE 'qwen3.*rerank|rerank.*qwen3' "$CONVERT"; then
    die 3 "llama.cpp $LC_VER's convert_hf_to_gguf.py has no Qwen3-Reranker head handling (#16407); it would write a ~1e-28-scoring GGUF"
fi

# ── 2. conversion venv from the tag's own requirements ─────────────────────
log "Creating the conversion venv..."
"$PYBIN" -m venv "$VENV" || die 4 "venv creation failed"
"$VENV/bin/pip" install -r "$LC_DIR/requirements/requirements-convert_hf_to_gguf.txt" \
    || die 4 "installing llama.cpp's convert requirements failed"

# ── 3. the HF checkpoint at the pinned revision ─────────────────────────────
log "Downloading $HF_REPO at $HF_REV..."
if ! "$VENV/bin/python" - "$HF_REPO" "$HF_REV" "$HF_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, rev, dest = sys.argv[1:4]
snapshot_download(repo_id=repo, revision=rev, local_dir=dest)
PY
then
    die 4 "download of the HF checkpoint failed"
fi

# ── 4. f16, then Q8_0 (the output name is load-bearing: llama-swap loads it) ─
log "Converting to f16..."
"$VENV/bin/python" "$CONVERT" "$HF_DIR" --outfile "$F16" --outtype f16 \
    || die 4 "convert_hf_to_gguf.py failed"
[[ -s "$F16" ]] || die 4 "the f16 conversion produced no file"
rm -rf "$HF_DIR"
log "Building llama-quantize..."
cmake -S "$LC_DIR" -B "$LC_DIR/build" -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release \
    || die 4 "cmake configure failed"
cmake --build "$LC_DIR/build" --target llama-quantize -j"$(nproc)" \
    || die 4 "building llama-quantize failed"
QUANTIZE="$(find "$LC_DIR/build" -type f -name llama-quantize | head -n 1)"
[[ -n "$QUANTIZE" ]] || die 4 "llama-quantize missing after the build"
log "Quantizing to Q8_0..."
"$QUANTIZE" "$F16" "$PART" Q8_0 || die 4 "llama-quantize failed"
rm -f "$F16"

# ── 5. the reranker head must have survived (runbook step 5) ────────────────
if ! PYTHONPATH="$LC_DIR/gguf-py" "$VENV/bin/python" - "$PART" <<'PY'
import sys
from gguf import GGUFReader
names = {t.name for t in GGUFReader(sys.argv[1]).tensors}
sys.exit(0 if "cls.output.weight" in names else 1)
PY
then
    die 3 "the Q8_0 GGUF has no cls.output.weight tensor: a broken conversion, not installed"
fi

SHA="$(sha256sum "$PART" | awk '{print $1}')"
MATCH=false
[[ "$SHA" == "$REF_SHA" ]] && MATCH=true
mv -f "$PART" "$OUT"
"$PYBIN" - "$OUT.provenance.json" "$LC_VER" "$HF_REPO" "$HF_REV" "$SHA" "$MATCH" <<'PY'
import json, sys, datetime
path, tag, repo, rev, sha, match = sys.argv[1:7]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"llama_cpp": tag, "hf_repo": repo, "hf_revision": rev, "sha256": sha,
               "matches_g2_reference": match == "true",
               "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
              f, indent=2)
    f.write("\n")
PY
log "Installed $OUT (sha256 $SHA)."
if [[ "$MATCH" == true ]]; then
    log "Byte-identical to the G2-validated MS02 artifact."
else
    log "Differs from the G2-validated reference ($REF_SHA): a different conversion."
    log "Run the G2 gate before selecting it:  Orchestrator/venv/bin/python eval/rerank_g2.py"
fi
log "Then select 'qwen3-reranker-8b-local' in the wizard (POST /rerank/select)."
