# Shared by the macOS installer and by the installed skill. Called from Bash.
d11_data_dir() { echo "${D11_DATA_DIR:-$HOME/Library/Application Support/d11-research}"; }
d11_python() {
  local mode="$1" data saved="" p
  data=$(d11_data_dir)
  if [ -f "$data/python-path" ]; then IFS= read -r saved < "$data/python-path" || true; fi
  for p in "${D11_PYTHON:-}" "$data/venv/bin/python3" "$saved" \
    "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" \
    "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/python/bin/python3" \
    "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    if [ "$p" = /usr/bin/python3 ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then continue; fi
    if [ "$mode" = ready ]; then
      "$p" -I -B -c 'import sys,PIL,docx; assert sys.version_info >= (3,10); assert (10,4) <= tuple(map(int,PIL.__version__.split(".")[:2])) < (14,); assert (1,1,2) <= tuple(map(int,docx.__version__.split(".")[:3])) < (2,)' >/dev/null 2>&1 || continue
    else
      "$p" -I -B -c 'import sys; assert sys.version_info >= (3,10)' >/dev/null 2>&1 || continue
    fi
    echo "$p"; return 0
  done
  return 1
}
