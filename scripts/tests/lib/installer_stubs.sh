#!/usr/bin/env bash
# Shared PATH stubs for the installer behaviour tests (2026-10 audit ranks 40/41).
#
# Sourced, never run. `stub_kit_init <work-dir>` writes fake sudo, systemctl,
# curl, nvidia-smi, python, git, cmake, apt and friends into <work-dir>/stubbin
# and points PATH at it, so an installer script or an extracted install.sh
# function runs end to end inside a mktemp sandbox:
#   - nothing is written under /etc or /usr: `sudo tee` and `sudo install` remap
#     an /etc/... or /usr/... destination into $STUB_ROOT (a fake root);
#   - nothing reaches the network: curl answers loopback health probes and
#     fails anything else unless the test provides a handler;
#   - every privileged or external call is appended to $STUB_LOG, one line per
#     call, so a test can assert what ran, as whom (`sudo -u <user>` exports
#     STUB_SUDO_USER to the child) and with which arguments.
# Knobs (environment, read at call time):
#   STUB_ENABLED_UNITS   space-separated units `systemctl is-enabled` reports enabled
#                        (on top of those a `systemctl enable` in this kit
#                        recorded in $STUB_UNITS; `disable` removes them there,
#                        and `: > "$STUB_UNITS"` forgets them all)
#   STUB_VRAM_MB         nvidia-smi memory.total (default 16380)
#   STUB_COMPUTE_CAP     nvidia-smi compute_cap, one GPU per line (default 8.9)
#   STUB_NVIDIA_SMI_FAIL exit code every nvidia-smi call fails with, printing the
#                        driver-dead message (unset: the driver answers)
#   STUB_LSPCI           lspci output (default: one NVIDIA VGA line; "" = no card)
#   STUB_NVCC_ARCHS      `nvcc --list-gpu-arch` output, space-separated (default:
#                        Ubuntu 24.04's apt CUDA 12.0, compute_50 .. compute_90);
#                        "none" makes it fail like an nvcc without the option
#   STUB_CURL_HANDLER    executable run as `<handler> <url> <output-path>` for
#                        non-loopback URLs; its exit code is curl's
#   STUB_DPKG_OWNER      package `dpkg -S` reports for /usr/bin/blackbox-setup
#   STUB_DPKG_VERSION    version `dpkg-query -W` reports for that package
#   STUB_DEB_PACKAGE     Package field `dpkg-deb -f` reports for a built .deb

stub_kit_init() {
    local work="$1" real_python
    real_python="$(command -v python3)"
    STUB_BIN="$work/stubbin"
    STUB_ROOT="$work/fakeroot"
    STUB_LOG="$work/stub.log"
    STUB_UNITS="$work/stub-enabled-units"
    mkdir -p "$STUB_BIN" "$STUB_ROOT"
    : > "$STUB_LOG"
    : > "$STUB_UNITS"
    export STUB_BIN STUB_ROOT STUB_LOG STUB_UNITS
    export PATH="$STUB_BIN:$PATH"

    # sudo: drop -n/-E, honour -u USER (exported as STUB_SUDO_USER), remap
    # root-only destinations of tee/install into the fake root, and otherwise
    # run the command as the invoking (unprivileged) test user.
    cat > "$STUB_BIN/sudo" <<'STUB'
#!/usr/bin/env bash
user=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|-E|-H) shift ;;
        -u) user="$2"; shift 2 ;;
        *) break ;;
    esac
done
echo "sudo${user:+ -u $user} $*" >> "$STUB_LOG"
[[ -n "$user" ]] && export STUB_SUDO_USER="$user"
remap() {
    case "$1" in
        /etc/*|/usr/*) mkdir -p "$(dirname "$STUB_ROOT$1")"; printf '%s' "$STUB_ROOT$1" ;;
        *) printf '%s' "$1" ;;
    esac
}
case "${1:-}" in
    tee)
        shift
        args=()
        for a in "$@"; do
            case "$a" in -*) args+=("$a") ;; *) args+=("$(remap "$a")") ;; esac
        done
        exec tee "${args[@]}" ;;
    install)
        shift
        args=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -o|-g) shift 2 ;;
                -m) args+=("$1" "$2"); shift 2 ;;
                -d) args+=("$1"); shift ;;
                *) args+=("$1"); shift ;;
            esac
        done
        last=$(( ${#args[@]} - 1 ))
        args[last]="$(remap "${args[last]}")"
        exec install "${args[@]}" ;;
    chown) exit 0 ;;
esac
exec "$@"
STUB

    cat > "$STUB_BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
args=()
for a in "$@"; do [[ "$a" == --* ]] || args+=("$a"); done
case "${args[0]:-}" in
    is-enabled)
        for u in ${STUB_ENABLED_UNITS:-}; do
            if [[ "$u" == "${args[1]:-}" ]]; then echo enabled; exit 0; fi
        done
        if grep -qxF -- "${args[1]:-}" "$STUB_UNITS" 2>/dev/null; then echo enabled; exit 0; fi
        echo disabled; exit 1 ;;
    enable)
        for u in "${args[@]:1}"; do
            grep -qxF -- "$u" "$STUB_UNITS" 2>/dev/null || echo "$u" >> "$STUB_UNITS"
        done ;;
    disable)
        for u in "${args[@]:1}"; do
            grep -vxF -- "$u" "$STUB_UNITS" > "$STUB_UNITS.tmp" 2>/dev/null || true
            mv "$STUB_UNITS.tmp" "$STUB_UNITS"
        done ;;
    is-failed) exit 1 ;;
esac
exit 0
STUB

    cat > "$STUB_BIN/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
url="" out="" wout=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output) out="$2"; shift 2 ;;
        -w|--write-out) wout="$2"; shift 2 ;;
        --max-time|--proto|-m) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
case "$url" in
    http://127.0.0.1:*|http://localhost:*)
        [[ -n "$wout" ]] && printf '200'
        exit 0 ;;
esac
if [[ -n "${STUB_CURL_HANDLER:-}" ]]; then
    exec "$STUB_CURL_HANDLER" "$url" "$out"
fi
exit 6
STUB

    cat > "$STUB_BIN/nvidia-smi" <<'STUB'
#!/usr/bin/env bash
echo "nvidia-smi $*" >> "$STUB_LOG"
if [[ -n "${STUB_NVIDIA_SMI_FAIL:-}" ]]; then
    echo "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."
    exit "$STUB_NVIDIA_SMI_FAIL"
fi
case "$*" in
    -L) echo "GPU 0: Stub NVIDIA GPU (UUID: GPU-00000000-0000-0000-0000-000000000000)" ;;
    *memory.total*) echo "${STUB_VRAM_MB:-16380}" ;;
    *compute_cap*) printf '%s\n' "${STUB_COMPUTE_CAP:-8.9}" ;;
esac
STUB

    # lspci: the NVIDIA card the nvidia-smi stub stands for, so the GPU
    # preflight sees the same box on every host.
    cat > "$STUB_BIN/lspci" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "${STUB_LSPCI-01:00.0 VGA compatible controller: NVIDIA Corporation Stub GPU (rev a1)}"
STUB

    # python3.12 / python3: `-m venv DIR` builds a stub venv whose pip logs its
    # arguments; anything else is the real interpreter.
    {
        echo '#!/usr/bin/env bash'
        printf 'REAL_PYTHON=%q\n' "$real_python"
        cat <<'STUB'
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
    echo "python -m venv ${3:-}" >> "$STUB_LOG"
    mkdir -p "$3/bin"
    printf '#!/usr/bin/env bash\necho "pip[%s] $*" >> "$STUB_LOG"\n' "$3" > "$3/bin/pip"
    printf '#!/usr/bin/env bash\necho "venv-python[%s] $*" >> "$STUB_LOG"\n' "$3" > "$3/bin/python"
    chmod +x "$3/bin/pip" "$3/bin/python"
    exit 0
fi
exec "$REAL_PYTHON" "$@"
STUB
    } > "$STUB_BIN/python3.12"
    cp "$STUB_BIN/python3.12" "$STUB_BIN/python3"

    # git clone ... <dir>: an empty checkout; cmake --build <dir>: a fake
    # llama-server plus one shared library under <dir>/bin.
    cat > "$STUB_BIN/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
if [[ "${1:-}" == "clone" ]]; then
    for last; do :; done
    mkdir -p "$last"
fi
exit 0
STUB
    cat > "$STUB_BIN/cmake" <<'STUB'
#!/usr/bin/env bash
echo "cmake $*" >> "$STUB_LOG"
if [[ "${1:-}" == "--build" ]]; then
    mkdir -p "$2/bin"
    printf '#!/bin/sh\n' > "$2/bin/llama-server"; chmod +x "$2/bin/llama-server"
    : > "$2/bin/libllama.so"
fi
exit 0
STUB

    cat > "$STUB_BIN/nvcc" <<'STUB'
#!/usr/bin/env bash
echo "nvcc $*" >> "$STUB_LOG"
if [[ "${1:-}" == "--list-gpu-arch" ]]; then
    archs="${STUB_NVCC_ARCHS-compute_50 compute_52 compute_53 compute_60 compute_61 compute_62 compute_70 compute_72 compute_75 compute_80 compute_86 compute_87 compute_89 compute_90}"
    [[ "$archs" == none ]] && { echo "nvcc fatal   : Unknown option '--list-gpu-arch'" >&2; exit 1; }
    printf '%s\n' $archs
fi
exit 0
STUB

    local simple
    for simple in apt apt-get sleep; do
        printf '#!/usr/bin/env bash\necho "%s $*" >> "$STUB_LOG"\nexit 0\n' "$simple" \
            > "$STUB_BIN/$simple"
    done

    cat > "$STUB_BIN/dpkg" <<'STUB'
#!/usr/bin/env bash
echo "dpkg $*" >> "$STUB_LOG"
if [[ "${1:-}" == "-S" ]]; then
    [[ -n "${STUB_DPKG_OWNER:-}" ]] || exit 1
    echo "${STUB_DPKG_OWNER}: $2"; exit 0
fi
exit 0
STUB
    cat > "$STUB_BIN/dpkg-query" <<'STUB'
#!/usr/bin/env bash
echo "dpkg-query $*" >> "$STUB_LOG"
[[ -n "${STUB_DPKG_VERSION:-}" ]] || exit 1
printf '%s' "$STUB_DPKG_VERSION"
STUB
    cat > "$STUB_BIN/dpkg-deb" <<'STUB'
#!/usr/bin/env bash
echo "dpkg-deb $*" >> "$STUB_LOG"
printf '%s\n' "${STUB_DEB_PACKAGE:-black-box-setup}"
STUB

    chmod +x "$STUB_BIN"/*
}

# Fail unless $STUB_LOG has a line matching the ERE.
stub_log_has() { grep -qE "$1" "$STUB_LOG"; }
