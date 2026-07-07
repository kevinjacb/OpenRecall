#!/usr/bin/env bash
# scripts/install_idf_5.1.6.sh — one-shot install + reconfigure for the
# IDF 5.1.6 downgrade path. See ~/.claude/plans/wobbly-tickling-salamander.md
# (Context section) for the full rationale.
#
# This script:
#   1. Clones ESP-IDF v5.1.6 to ~/esp/esp-idf-v5.1.6/ (idempotent)
#   2. Runs ./install.sh esp32s3 to fetch the S3 toolchain and Python env
#   3. Asks before deleting project state from a 6.0.1 build (sdkconfig,
#      dependencies.lock, managed_components/, build/) — these must be
#      regenerated against the 5.1.6 Kconfig tree
#   4. Sources the 5.1.6 env and runs `idf.py set-target esp32s3` and
#      `idf.py reconfigure` to regenerate sdkconfig
#   5. Optionally runs `idf.py build` at the end
#
# Side-by-side: does NOT touch /Users/kevin/.espressif/v6.0.1/. The v6.0.1
# install keeps working in a different shell.

set -euo pipefail

# --- Paths ---------------------------------------------------------------
IDF_TAG="v5.1.6"
IDF_PARENT="${HOME}/esp"
IDF_ROOT="${IDF_PARENT}/esp-idf-v5.1.6"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Where the rest of this script sees idf.py after install. Sourced in step 2
# and again at the end so subsequent commands in this shell pick it up.
IDF_EXPORT="${IDF_ROOT}/export.sh"

# --- Colors (only when stdout is a tty) ----------------------------------
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_RED=$'\033[31m'
  C_GRN=$'\033[32m'
  C_YLW=$'\033[33m'
  C_CYN=$'\033[36m'
  C_RST=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GRN=""; C_YLW=""; C_CYN=""; C_RST=""
fi

log()   { printf '%s[install_idf]%s %s\n' "$C_CYN" "$C_RST" "$*"; }
warn()  { printf '%s[install_idf] WARN:%s %s\n' "$C_YLW" "$C_RST" "$*" >&2; }
err()   { printf '%s[install_idf] ERROR:%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
bold()  { printf '%s%s%s\n' "$C_BOLD" "$*" "$C_RST"; }
dim()   { printf '%s%s%s\n' "$C_DIM"  "$*" "$C_RST"; }

# --- Preflight -----------------------------------------------------------
require() { command -v "$1" >/dev/null 2>&1 || { err "missing: $1"; exit 1; }; }
require git
require python3

bold ""
bold "Sense AI Sensor — IDF ${IDF_TAG} install"
bold "==========================================="
dim "  project:    ${PROJECT_DIR}"
dim "  IDF target: ${IDF_ROOT}"
dim "  ESP-IDF:    ${IDF_TAG} (NimBLE 1.6, last battle-tested 5.x for S3)"
dim ""

# --- Step 1: clone -------------------------------------------------------
if [ -d "${IDF_ROOT}/.git" ]; then
  log "ESP-IDF already cloned at ${IDF_ROOT}"
  log "checking out ${IDF_TAG} and updating submodules (idempotent)"
  (cd "${IDF_ROOT}" && git fetch --depth 1 --filter=blob:none origin "${IDF_TAG}" 2>/dev/null || true)
  (cd "${IDF_ROOT}" && git checkout -q "${IDF_TAG}" 2>/dev/null || git checkout -q -B "${IDF_TAG}" "origin/${IDF_TAG}")
  (cd "${IDF_ROOT}" && git submodule update --init --depth 1 --recursive -q)
else
  bold "Step 1/4 — clone ESP-IDF ${IDF_TAG}"
  mkdir -p "${IDF_PARENT}"
  git clone -q -b "${IDF_TAG}" --depth 1 --recurse-submodules --shallow-submodules \
      https://github.com/espressif/esp-idf.git "${IDF_ROOT}"
  log "cloned to ${IDF_ROOT}"
fi

# --- Step 2: install toolchain + python env ------------------------------
if [ -x "${IDF_ROOT}/install.sh" ]; then
  bold ""
  bold "Step 2/4 — install ESP32-S3 toolchain + Python env"
  dim "  (this takes 3-5 min the first time; the .espressif/python_env/ venv"
  dim "   and tools/xtensa-esp-elf/ live alongside the v6.0.1 install)"
  (cd "${IDF_ROOT}" && ./install.sh esp32s3)
else
  err "install.sh missing at ${IDF_ROOT}/install.sh — clone failed?"
  exit 1
fi

# --- Step 3: project reset (asks first) ----------------------------------
bold ""
bold "Step 3/4 — project reset for ${IDF_TAG}"
dim "  This deletes the 6.0.1-derived sdkconfig, dependencies.lock,"
dim "  managed_components/ and build/ so the 5.1.6 Kconfig tree can"
dim "  regenerate them. Source under main/ and components/opus/ is NOT touched."

# Show what's about to be deleted
TO_DELETE=()
[ -f "${PROJECT_DIR}/sdkconfig" ] && TO_DELETE+=("sdkconfig")
[ -f "${PROJECT_DIR}/sdkconfig.old" ] && TO_DELETE+=("sdkconfig.old")
[ -f "${PROJECT_DIR}/dependencies.lock" ] && TO_DELETE+=("dependencies.lock")
[ -d "${PROJECT_DIR}/managed_components" ] && TO_DELETE+=("managed_components/")
[ -d "${PROJECT_DIR}/build" ] && TO_DELETE+=("build/")

if [ ${#TO_DELETE[@]} -eq 0 ]; then
  log "nothing to reset (project is already clean)"
else
  warn "About to delete:"
  for f in "${TO_DELETE[@]}"; do printf '    %s\n' "$f"; done
  if [ -t 0 ]; then
    read -r -p "  Proceed? [y/N] " ans
    case "$ans" in
      y|Y|yes|YES) ;;
      *) err "aborted"; exit 1 ;;
    esac
  else
    err "non-interactive shell — re-run from a terminal to confirm"
    exit 1
  fi
  cd "${PROJECT_DIR}"
  rm -f sdkconfig sdkconfig.old dependencies.lock
  rm -rf managed_components build
  log "project reset"
fi

# --- Step 4: reconfigure + (optional) build ------------------------------
bold ""
bold "Step 4/4 — set-target esp32s3 + reconfigure"
# Source the IDF env so `idf.py` is on PATH and IDF_PATH / IDF_TOOLS_PATH are set.
# shellcheck disable=SC1091
. "${IDF_EXPORT}"

# Sanity check: confirm the env switch actually took. If a previous IDF
# version's PATH entry is in front, the wrong `idf.py` will run, and the
# user will see a confusing mix of v5.1.6's CMake errors and v6.0.1's Python
# errors. Bail loudly so they know to start a fresh shell.
IDF_VERSION_ACTUAL="$(idf.py --version 2>/dev/null || echo unknown)"
case "${IDF_VERSION_ACTUAL}" in
  *"v${IDF_TAG#v}"*|*"${IDF_TAG#v}"*)
    log "idf.py reports: ${IDF_VERSION_ACTUAL} (expected ${IDF_TAG})"
    ;;
  *)
    err "expected idf.py to report ${IDF_TAG}, got: ${IDF_VERSION_ACTUAL}"
    err "this usually means a different ESP-IDF env is still active in this"
    err "shell. Open a new terminal and run the script again (the env"
    err "variables from a prior 'source activate_idf_v6.0.1.sh' take"
    err "precedence over what export.sh sets)."
    exit 1
    ;;
esac

cd "${PROJECT_DIR}"
idf.py set-target esp32s3
idf.py reconfigure

bold ""
bold "==========================================="
log "ESP-IDF ${IDF_TAG} is now active for ${PROJECT_DIR}"
log ""
log "Next steps:"
log "  1. (Optional) idf.py build              # compile the firmware"
log "  2. idf.py -p /dev/cu.usbmodem* flash     # flash the XIAO"
log "  3. idf.py -p /dev/cu.usbmodem* monitor   # watch boot logs"
log ""
log "To re-enter this shell's IDF env in a new terminal:"
log "  source ${IDF_EXPORT}"
log "The v6.0.1 install is untouched and still works:"
log "  source /Users/kevin/.espressif/tools/activate_idf_v6.0.1.sh"

# --- Optional: kick off the build ---------------------------------------
if [ -t 0 ]; then
  read -r -p "Run 'idf.py build' now? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES)
      bold ""
      bold "Building..."
      idf.py build
      ;;
    *) log "skipping build (run 'idf.py build' when ready)";;
  esac
fi
