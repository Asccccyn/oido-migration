#!/bin/bash
# oído environment check (WP00 §5 / REVIEW_20260929 ruling 2: pin ffmpeg version)
set -u

fail=0
check() {
  local name="$1"; shift
  if command -v "$name" >/dev/null 2>&1; then
    echo "OK   $name: $($name --version 2>&1 | head -1 | cut -c1-80)"
  else
    echo "MISS $name ($*)"; fail=1
  fi
}

echo "== oido env check $(date '+%F %T') =="
check python3.12 "Homebrew python3.12 (venv base)"
check ffmpeg "media normalization tool (9.0.2 pinned; argv+timeout+limits only)"
check node "estomago viewer / probe"
echo "venv: $([ -x .venv/bin/python ] && echo 'OK' || echo 'MISS (python3.12 -m venv .venv)')"
echo "lock: $([ -f requirements.lock ] && echo "OK ($(wc -l < requirements.lock | tr -d ' ') entries)" || echo MISS)"

# ffmpeg version pin (REVIEW ruling 2)
FFV=$(ffmpeg -version 2>/dev/null | head -1 | grep -o 'version [0-9.]*' | cut -d' ' -f2)
if [ "${FFV:-}" = "9.0.2" ]; then
  echo "ffmpeg-version: OK ($FFV, matches registry)"
else
  echo "ffmpeg-version: NOTE (found ${FFV:-none}, registry says 9.0.2; if upgraded, re-verify decode behavior and update this script + docs)"
fi

exit $fail
