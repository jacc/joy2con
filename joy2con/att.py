# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Bluetooth LE ATT client over a raw L2CAP socket.

The Joy-Con 2 does not finish GATT discovery through bluetoothd, so this talks
the ATT protocol directly on the fixed ATT channel (CID 4). A reader thread
splits incoming PDUs into notifications (passed to a callback) and responses
(handed to the request that is waiting for one).
"""

from __future__ import annotations

import ctypes
import logging
import select
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
ATT_CID = 4
BDADDR_LE_PUBLIC = 1
BDADDR_LE_RANDOM = 2

OP_ERROR = 0x01
OP_MTU_REQ = 0x02
OP_FIND_INFO_REQ = 0x04
OP_FIND_INFO_RSP = 0x05
OP_READ_BY_TYPE_REQ = 0x08
OP_READ_BY_TYPE_RSP = 0x09
OP_READ_REQ = 0x0A
OP_READ_BY_GROUP_REQ = 0x10
OP_READ_BY_GROUP_RSP = 0x11
OP_WRITE_REQ = 0x12
OP_WRITE_CMD = 0x52
OP_NOTIFY = 0x1B
OP_INDICATE = 0x1D
OP_CONFIRM = 0x1E

ERR_ATTR_NOT_FOUND = 0x0A

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


class ATTError(Exception):
    def __init__(self, op: int, handle: int, code: int) -> None:
        super().__init__(f"ATT error: request {op:#04x} handle {handle:#06x} code {code:#04x}")
        self.op, self.handle, self.code = op, handle, code


@dataclass
class Characteristic:
    uuid: str
    props: int
    value_handle: int
    cccd: int | None = None
    descriptors: dict[str, int] = field(default_factory=dict)  # descriptor UUID -> handle


@dataclass
class Service:
    uuid: str
    start: int
    end: int
    chars: list[Characteristic] = field(default_factory=list)


def _bdaddr(mac: str) -> bytes:
    return bytes(int(x, 16) for x in reversed(mac.split(":")))


def _sockaddr(mac: str, addr_type: int) -> bytes:
    # struct sockaddr_l2: family, psm, bdaddr, cid, bdaddr_type
    return struct.pack("<HH6sHB", AF_BLUETOOTH, 0, _bdaddr(mac), ATT_CID, addr_type) + b"\0"


def uuid_str(raw: bytes) -> str:
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    b = raw[::-1].hex()
    return f"{b[0:8]}-{b[8:12]}-{b[12:16]}-{b[16:20]}-{b[20:32]}"


class ATTClient:
    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.sock: socket.socket | None = None
        self.mtu = 23
        self.on_notify: Callable[[int, bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._resp: bytes | None = None
        self._closing = False
        self.notify_errors = 0

    def connect(self, timeout: float = 10.0, types: tuple[int, ...] = (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM)) -> None:
        errors = []
        for addr_type in types:
            err = self._try_connect(addr_type, timeout)
            if err is None:
                return
            errors.append(f"{'public' if addr_type == 1 else 'random'}: {err}")
        raise ConnectionError("; ".join(errors))

    def _try_connect(self, addr_type: int, timeout: float) -> str | None:
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_LOW, 0))
        # Binding to the any-address lets the kernel route through the default adapter.
        local = _sockaddr("00:00:00:00:00:00", BDADDR_LE_PUBLIC)
        if _libc.bind(s.fileno(), local, len(local)) != 0:
            s.close()
            return f"bind errno {ctypes.get_errno()}"
        s.setblocking(False)
        remote = _sockaddr(self.mac, addr_type)
        if _libc.connect(s.fileno(), remote, len(remote)) != 0:
            e = ctypes.get_errno()
            if e not in (114, 115):  # EALREADY, EINPROGRESS
                s.close()
                return f"connect errno {e}"
        _, writable, _ = select.select([], [s], [], timeout)
        if not writable:
            s.close()
            return "timeout"
        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            s.close()
            return f"SO_ERROR {err}"
        s.setblocking(True)
        self.sock = s
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            rsp = self.request(struct.pack("<BH", OP_MTU_REQ, 512))
            self.mtu = min(512, struct.unpack_from("<H", rsp, 1)[0])
        except (ATTError, TimeoutError):
            pass
        return None

    def close(self) -> None:
        self._closing = True
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _reader(self) -> None:
        sock = self.sock
        while sock is not None:
            try:
                pdu = sock.recv(1024)
            except OSError:
                break
            if not pdu:
                break
            op = pdu[0]
            if op in (OP_NOTIFY, OP_INDICATE):
                if op == OP_INDICATE:
                    sock.send(bytes([OP_CONFIRM]))
                if self.on_notify:
                    # A failure while handling one notification must not end
                    # the link: log it and keep reading.
                    try:
                        self.on_notify(struct.unpack_from("<H", pdu, 1)[0], pdu[3:])
                    except Exception:
                        self.notify_errors += 1
                        if self.notify_errors <= 5 or self.notify_errors % 1000 == 0:
                            log.exception("error handling notification (%d so far)", self.notify_errors)
            else:
                self._resp = pdu
                self._event.set()
        if not self._closing and self.on_disconnect:
            self.on_disconnect()

    def request(self, pdu: bytes, timeout: float = 3.0) -> bytes:
        with self._lock:
            self._event.clear()
            self.sock.send(pdu)
            if not self._event.wait(timeout):
                raise TimeoutError(f"no response to ATT request {pdu[0]:#04x}")
            rsp = self._resp
        if rsp[0] == OP_ERROR:
            _, op, handle, code = struct.unpack_from("<BBHB", rsp)
            raise ATTError(op, handle, code)
        return rsp

    def read(self, handle: int) -> bytes:
        return self.request(struct.pack("<BH", OP_READ_REQ, handle))[1:]

    def write(self, handle: int, value: bytes) -> None:
        """Write with response (ATT Write Request)."""
        self.request(struct.pack("<BH", OP_WRITE_REQ, handle) + value)

    def write_cmd(self, handle: int, value: bytes) -> None:
        """Write without response (ATT Write Command)."""
        self.sock.send(struct.pack("<BH", OP_WRITE_CMD, handle) + value)

    def enable_notify(self, cccd: int) -> None:
        self.write(cccd, b"\x01\x00")

    def _iterate(self, op: int, start: int, end: int, type16: int | None):
        """Yield raw entries of a Read By Group/Type or Find Information walk."""
        while start <= end:
            if type16 is None:
                pdu = struct.pack("<BHH", op, start, end)
            else:
                pdu = struct.pack("<BHHH", op, start, end, type16)
            try:
                rsp = self.request(pdu)
            except ATTError as e:
                if e.code == ERR_ATTR_NOT_FOUND:
                    return
                raise
            if op == OP_FIND_INFO_REQ:
                size = 4 if rsp[1] == 1 else 18
            else:
                size = rsp[1]
            data = rsp[2:]
            last = start
            for i in range(0, len(data) - size + 1, size):
                entry = data[i:i + size]
                yield entry
                last = struct.unpack_from("<H", entry, 2 if op == OP_READ_BY_GROUP_REQ else 0)[0]
            if last >= end:
                return
            start = last + 1

    def discover(self) -> list[Service]:
        services = []
        for e in self._iterate(OP_READ_BY_GROUP_REQ, 1, 0xFFFF, 0x2800):
            s, end = struct.unpack_from("<HH", e)
            services.append(Service(uuid_str(e[4:]), s, end))
        for svc in services:
            for e in self._iterate(OP_READ_BY_TYPE_REQ, svc.start, svc.end, 0x2803):
                props = e[2]
                vh = struct.unpack_from("<H", e, 3)[0]
                svc.chars.append(Characteristic(uuid_str(e[5:]), props, vh))
            for i, ch in enumerate(svc.chars):
                stop = svc.chars[i + 1].value_handle - 2 if i + 1 < len(svc.chars) else svc.end
                if stop <= ch.value_handle:
                    continue
                for e in self._iterate(OP_FIND_INFO_REQ, ch.value_handle + 1, stop, None):
                    handle = struct.unpack_from("<H", e)[0]
                    uuid = uuid_str(e[2:])
                    ch.descriptors[uuid] = handle
                    if uuid == "2902":
                        ch.cccd = handle
        return services
