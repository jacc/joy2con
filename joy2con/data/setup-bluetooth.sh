#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# One-time system setup for joy2con. Run as root (pkexec or sudo).
#
# 1. LE connection interval 7.5 ms (the Bluetooth minimum). The Joy-Con 2 never
#    asks for a faster interval itself, so the host default decides its report
#    rate: BlueZ's default (30-50 ms) gives ~20 reports/s, 7.5 ms gives ~133.
# 2. BlueZ experimental interfaces, which provide the Advertisement Monitor
#    API. joy2con uses it to wait for a sleeping Joy-Con's reconnect
#    advertisement with a low-duty passive scan instead of a full active scan,
#    which would compete with the connected Joy-Con for radio time.
#
# A copy of the original file is kept at main.conf.joy2con.bak.
set -euo pipefail

conf=/etc/bluetooth/main.conf
[[ -f $conf.joy2con.bak ]] || cp -a "$conf" "$conf.joy2con.bak"

set_key() {  # set_key SECTION KEY VALUE: uncomment or replace KEY, else add it to [SECTION]
    local section=$1 key=$2 value=$3
    if grep -qE "^#?\s*$key\s*=" "$conf"; then
        sed -i -E "s|^#?\s*$key\s*=.*|$key = $value|" "$conf"
    elif grep -qE "^\[$section\]" "$conf"; then
        sed -i -E "/^\[$section\]/a $key = $value" "$conf"
    else
        printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$conf"
    fi
}

set_key General Experimental true
set_key LE MinConnectionInterval 6
set_key LE MaxConnectionInterval 6
set_key LE ConnectionLatency 0
set_key LE ScanIntervalAdvMonitor 160
set_key LE ScanWindowAdvMonitor 16

systemctl restart bluetooth
echo "joy2con: Bluetooth configured:"
grep -nE '^(Experimental|MinConnectionInterval|MaxConnectionInterval|ConnectionLatency|ScanIntervalAdvMonitor|ScanWindowAdvMonitor)\b' "$conf"
