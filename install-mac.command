#!/bin/bash
# Double-click on macOS, or: bash install-mac.command --host codex|workbuddy|both
set -euo pipefail
cd "$(dirname "$0")"
[ "$(uname -s)" = Darwin ] || { echo '本版只支持 macOS。' >&2; exit 2; }
. ./skills/d11-research/scripts/runtime.sh
usage='用法：bash install-mac.command [--host codex|workbuddy|both] [--project 文件夹] [--managed-python]'
managed=0
opts=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --managed-python) managed=1; shift;;
    --host|--project) [ "$#" -ge 2 ] || { echo "$usage" >&2; exit 2; }; opts+=("$1" "$2"); shift 2;;
    --help|-h) echo "$usage"; exit 0;;
    *) echo "$usage" >&2; exit 2;;
  esac
done
py=""
if [ "$managed" = 0 ]; then py=$(d11_python ready || d11_python base || true); fi
if [ -z "$py" ]; then
  # Pinned official release; do not source a remote shell installer or edit PATH.
  case "$(uname -m)" in
    arm64) target=aarch64-apple-darwin; sum=81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731;;
    x86_64) target=x86_64-apple-darwin; sum=20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7;;
    *) echo '当前 Mac 芯片不在支持范围。' >&2; exit 2;;
  esac
  data=$(d11_data_dir)
  mkdir -p "$data"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/d11-bootstrap.XXXXXXXX")
  trap 'rm -rf -- "$tmp"' EXIT
  echo '正在下载本工具专用的 Python 3.12（只在当前用户目录安装）…'
  /usr/bin/curl --proto '=https' --proto-redir '=https' -fsSL --retry 2 --max-time 180 \
    "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-$target.tar.gz" -o "$tmp/uv.tar.gz"
  actual=$(/usr/bin/shasum -a 256 "$tmp/uv.tar.gz"); actual=${actual%% *}
  [ "$actual" = "$sum" ] || { echo '下载校验不一致，已停止。' >&2; exit 3; }
  /usr/bin/tar -xzf "$tmp/uv.tar.gz" -C "$tmp" "uv-$target/uv"
  export UV_PYTHON_INSTALL_DIR="$data/python" UV_PYTHON_BIN_DIR="$data/bin" UV_CACHE_DIR="$data/uv-cache"
  "$tmp/uv-$target/uv" python install 3.12
  py=$("$tmp/uv-$target/uv" python find --managed-python 3.12)
fi
# Bash 3.2 expands an empty array as unbound under set -u.
if [ "${#opts[@]}" -gt 0 ]; then "$py" -B install.py "${opts[@]}"; else "$py" -B install.py; fi
