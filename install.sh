#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Installs joy2con for the current user from this checkout, without root:
#   ~/.local/share/joy2con/venv          Python environment (sees system packages,
#                                         so the app uses the system's PySide6 and KDE modules)
#   ~/.local/bin/joy2con                 the command
#   ~/.config/systemd/user/joy2con.service
#   ~/.local/share/applications/joy2con.desktop and the icon
#
#   ./install.sh               install or update
#   ./install.sh --uninstall   remove all of the above (keeps ~/.config/joy2con)
set -euo pipefail

src=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
config=${XDG_CONFIG_HOME:-$HOME/.config}
prefix=$data/joy2con
bin=$HOME/.local/bin
unit=$config/systemd/user/joy2con.service
desktop=$data/applications/joy2con.desktop
icon=$data/icons/hicolor/scalable/apps/joy2con.svg

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*"; }

if [[ ${1:-} == --uninstall ]]; then
    systemctl --user disable --now joy2con.service 2>/dev/null || true
    rm -rf "$prefix" "$bin/joy2con" "$unit" "$desktop" "$icon"
    systemctl --user daemon-reload 2>/dev/null || true
    say "joy2con removed. Your settings in $config/joy2con were kept."
    exit 0
fi

python3 - <<'EOF' || { echo "joy2con needs Python 3.11 or newer." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

say "Installing joy2con into $prefix"
python3 -m venv --system-site-packages --clear "$prefix/venv"
"$prefix/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$src"
mkdir -p "$bin"
ln -sf "$prefix/venv/bin/joy2con" "$bin/joy2con"

mkdir -p "$(dirname "$unit")"
cat > "$unit" <<EOF
[Unit]
Description=joy2con: Switch 2 Joy-Con driver
Documentation=https://github.com/jacc/joy2con
After=bluetooth.target

[Service]
ExecStart=$prefix/venv/bin/joy2con daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

mkdir -p "$(dirname "$desktop")" "$(dirname "$icon")"
sed "s|^Exec=joy2con|Exec=$prefix/venv/bin/joy2con|" "$src/data/joy2con.desktop" > "$desktop"
install -m644 "$src/joy2con/data/joy2con.svg" "$icon"

# Checks for things this script can't do itself.
if ! "$prefix/venv/bin/python" -c "import PySide6" 2>/dev/null; then
    warn "PySide6 isn't installed, so the app won't start (the driver works without it)."
    echo "  Install your distribution's PySide6 package, plus Kirigami, Kirigami Addons and qqc2-desktop-style."
fi
if [[ ! -w /dev/uhid ]]; then
    warn "you can't write to /dev/uhid yet, which joy2con needs to create controllers. Run once:"
    echo "  sudo install -Dm644 $src/data/70-joy2con.rules /etc/udev/rules.d/70-joy2con.rules"
    echo "  sudo udevadm control --reload && sudo udevadm trigger /dev/uhid"
fi
case ":$PATH:" in *":$bin:"*) ;; *) warn "$bin isn't on your PATH; add it to run 'joy2con' by name." ;; esac

say "Done. Next:"
echo "  joy2con setup                           # one-time Bluetooth settings (asks for your password)"
echo "  systemctl --user enable --now joy2con   # start the driver now and at every login"
echo "  joy2con                                 # open the app"
