#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

if [[ "${1:-}" == "--list-versions" ]]; then
  nf_list_versions neut
  exit 0
fi

nf_default_paths

INSTALL_DIR="$NF_SOFTWARE_ROOT/neut"
META_DIR="$INSTALL_DIR/metadata"
ensure_dir "$INSTALL_DIR"

cat > "$INSTALL_DIR/README.setup.txt" <<'EOF'
NEUT setup placeholder
======================

Automated NEUT source download is intentionally deferred in this first version.
Use this location to place a site-approved source bundle or prebuilt installation,
and then extend the unified runtime generator wrapper accordingly.
EOF

write_metadata "$META_DIR" \
  "generator=NEUT" \
  "status=manual-setup-required" \
  "note=automated download intentionally skipped in v0.1"

log "NEUT placeholder prepared at $INSTALL_DIR"
