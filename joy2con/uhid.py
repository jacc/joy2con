# SPDX-License-Identifier: GPL-3.0-or-later
"""User-space HID devices through /dev/uhid.

A device created here looks to the kernel like real HID hardware: kernel HID
drivers bind to it by bus/vendor/product, and it gets a hidraw node. Input
reports are written with UHID_INPUT2; reports the host sends to the device
(output reports, GET/SET_REPORT) arrive as events on the same file, read by a
background thread and passed to callbacks.

Struct layouts follow <linux/uhid.h>; struct uhid_event is packed.
"""

from __future__ import annotations

import errno
import os
import select
import struct
import threading
from typing import Callable

UHID_DESTROY = 1
UHID_START = 2
UHID_STOP = 3
UHID_OPEN = 4
UHID_CLOSE = 5
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14

UHID_FEATURE_REPORT = 0
UHID_OUTPUT_REPORT = 1
UHID_INPUT_REPORT = 2

BUS_USB = 0x03
BUS_BLUETOOTH = 0x05

DATA_MAX = 4096
EVENT_SIZE = 4 + 4372  # u32 type + largest union member (uhid_create2_req)


class UHIDDevice:
    """One virtual HID device. Callbacks run on the reader thread."""

    def __init__(self, *, name: str, phys: str, uniq: str, bus: int, vendor: int,
                 product: int, version: int, descriptor: bytes,
                 on_output: Callable[[bytes], None],
                 on_event: Callable[[int], None] | None = None) -> None:
        self.on_output = on_output
        self.on_event = on_event
        self.fd = os.open("/dev/uhid", os.O_RDWR | os.O_CLOEXEC)
        self._write_lock = threading.Lock()
        self._closed = False
        # Written to on close so the reader thread's select() returns.
        self._wake_r, self._wake_w = os.pipe2(os.O_CLOEXEC)
        self._reader = threading.Thread(target=self._read_loop, name=f"uhid-{name}", daemon=True)
        # The reader must run before CREATE2: the kernel driver probes the
        # device synchronously and its first subcommands need answers.
        self._reader.start()
        req = struct.pack(
            "<128s64s64sHHIIII",
            name.encode()[:127], phys.encode()[:63], uniq.encode()[:63],
            len(descriptor), bus, vendor, product, version, 0,
        ) + descriptor
        self._write(struct.pack("<I", UHID_CREATE2) + req)

    def _write(self, buf: bytes) -> None:
        with self._write_lock:
            if self._closed:
                return
            os.write(self.fd, buf)

    def send_input(self, report: bytes) -> None:
        """Deliver one input report (report ID first) to the kernel.

        Reports racing a close() are dropped: once UHID_DESTROY is written the
        kernel rejects input with EINVAL."""
        try:
            self._write(struct.pack("<IH", UHID_INPUT2, len(report)) + report)
        except OSError as e:
            if e.errno not in (errno.EINVAL, errno.ENODEV):
                raise

    def _read_loop(self) -> None:
        while True:
            ready, _, _ = select.select([self.fd, self._wake_r], [], [])
            if self._wake_r in ready:
                return
            try:
                ev = os.read(self.fd, EVENT_SIZE)
            except OSError as e:
                if e.errno in (errno.EINTR, errno.EAGAIN):
                    continue
                return
            if len(ev) < 4:
                return
            etype = struct.unpack_from("<I", ev)[0]
            body = ev[4:]
            if etype == UHID_OUTPUT:
                size, rtype = struct.unpack_from("<HB", body, DATA_MAX)
                if rtype == UHID_OUTPUT_REPORT:
                    self.on_output(bytes(body[:size]))
            elif etype == UHID_SET_REPORT:
                rid, rnum, rtype, size = struct.unpack_from("<IBBH", body)
                data = bytes(body[8:8 + size])
                # Some hosts send output reports as SET_REPORT; treat them the same.
                if rtype == UHID_OUTPUT_REPORT and data:
                    self.on_output(data)
                self._write(struct.pack("<IIH", UHID_SET_REPORT_REPLY, rid, 0))
            elif etype == UHID_GET_REPORT:
                rid = struct.unpack_from("<I", body)[0]
                # No feature or input reports can be fetched on demand.
                self._write(struct.pack("<IIHH", UHID_GET_REPORT_REPLY, rid, errno.EIO, 0))
            elif self.on_event:
                self.on_event(etype)

    def close(self) -> None:
        # Destroy and mark closed in one step, so no input write can reach the
        # kernel after the device is gone.
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.write(self.fd, struct.pack("<I", UHID_DESTROY))
            except OSError:
                pass
        os.write(self._wake_w, b"x")
        self._reader.join(timeout=1)
        os.close(self.fd)
        os.close(self._wake_r)
        os.close(self._wake_w)
