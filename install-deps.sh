#!/usr/bin/env bash
# wiivault dependencies.
#
# wiivault.py itself is pure Python 3 standard library — there is nothing to
# pip install. What it needs are external command-line tools for unpacking
# archives and converting disc images.
#
#   usage:  ./install-deps.sh          (Debian/Ubuntu/WSL)
#
set -euo pipefail

echo "Installing wiivault system dependencies (apt)..."
sudo apt-get update
sudo apt-get install -y \
    python3 \
    p7zip-full \
    wit \
    unrar

cat <<'EOF'

Done. Installed:
  python3      - runs wiivault.py (3.8+; stdlib only, no pip packages)
  p7zip-full   - `7z`, unpacks the .7z archives Vimm ships
  wit          - Wiimms ISO Tools: CISO/ISO -> WBFS or ISO, 4GB FAT32 splitting
  unrar        - .rar archives (optional; 7z covers most cases)

NOT installed automatically (optional, only for .nkit.iso sources):
  nkit         - Nanook/NKit, needs the .NET 6 runtime.
                 https://github.com/Nanook/NKit
                 Then point wiivault at it:  python3 wiivault.py config --nkit /path/to/nkit

Verify:
  python3 --version && 7z i >/dev/null && wit --version
EOF
