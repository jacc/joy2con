# Changelog

## 0.1.0b1 (first beta)

- Pairs Switch 2 Joy-Cons (L and R) over Bluetooth LE and reconnects them on a button press
- Presents them as one Pro Controller or two original Joy-Cons through `/dev/uhid`,
  for the kernel's `hid-nintendo` driver, Steam, SDL and Wine
- Motion at the full report rate with factory calibration and drift correction at rest
- Rumble, player lights and battery level
- KDE app and tray icon with live buttons, sticks, motion, battery and link statistics
- `install.sh` for a per-user install on any distribution, and an Arch package
