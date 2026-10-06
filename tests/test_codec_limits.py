"""imsg decoder hardening: hostile / malformed input must not exhaust us."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import imsg_codec as codec

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== decoder limits ===")

# A client that streams bytes which never complete a frame used to grow the
# buffer without bound. A frame can never exceed MAX_IMSGSIZE, so anything past
# that is refused.
d = codec.Decoder()
try:
    for _ in range(3):
        d.feed(b"\x00" * 8000)
    check("truncated-frame flood is refused", False)
except ValueError as e:
    check("truncated-frame flood is refused", "overflow" in str(e))

# Normal frames still decode.
d = codec.Decoder()
d.feed(codec.pack(207))                       # MSG_READY
msg = d.next_msg()
check("a normal frame still decodes", msg is not None and msg.type == 207)

# An over-long declared length is rejected outright.
d = codec.Decoder()
import struct
d.feed(struct.pack(codec.HDR_FMT, 200, codec.MAX_IMSGSIZE + 100, 8, 0))
try:
    d.next_msg()
    check("over-long declared length rejected", False)
except ValueError:
    check("over-long declared length rejected", True)

# Excess fds are closed rather than leaked into our process table.
d = codec.Decoder()
fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(12)]
d.feed(b"", fds)
check("excess fds are reclaimed, not leaked",
      len(d._fds) <= codec.Decoder.MAX_PENDING_FDS,
      f"({len(d._fds)} kept)")
d.close()
check("close() releases the rest", len(d._fds) == 0)

print("\n=== both imsg header layouts ===")

import struct  # noqa: E402

# tmux up to 3.5a bundles the older imsg: uint16 len + uint16 flags, with
# IMSGF_HASFD = 1. Read as the newer single uint32 that is 0x10010 = 65552 for
# a 16-byte fd-bearing frame -- "invalid imsg length 65552", and a 3.5a client
# could not even identify itself (seen live on mini.wsen.me).
def old_frame(msg_type, payload=b"", hasfd=False):
    length = codec.IMSG_HEADER_SIZE + len(payload)
    return struct.pack("=IHHII", msg_type, length, 1 if hasfd else 0,
                       codec.PROTOCOL_VERSION, 0xFFFFFFFF) + payload

r, w = os.pipe()
d = codec.Decoder()
d.feed(old_frame(104, hasfd=True), [r])
m = d.next_msg()
check("old layout: fd-bearing frame decodes (was 'invalid length 65552')",
      m is not None and m.type == 104 and m.payload == b"")
check("...and claims its fd", m is not None and m.fd == r)
os.close(r); os.close(w)

d = codec.Decoder()
d.feed(old_frame(200, b"hello\0"))
m = d.next_msg()
check("old layout: plain frame decodes with its payload",
      m is not None and m.payload == b"hello\0" and m.fd is None)

r, w = os.pipe()
d = codec.Decoder()
d.feed(codec.pack(104, with_fd=True), [r])
m = d.next_msg()
check("new layout: IMSG_FD_MARK frame still decodes and claims its fd",
      m is not None and m.type == 104 and m.fd == r)
os.close(r); os.close(w)

check("frames we send read the same in both layouts (upper 16 bits zero)",
      struct.unpack("=IHHII", codec.pack(207, b"x")[:16])[1:3] == (17, 0))

d = codec.Decoder()
d.feed(struct.pack("=IIII", 104, 0x00220010, 8, 0))
try:
    d.next_msg()
    rejected = False
except ValueError:
    rejected = True
check("junk in the flag bits is still rejected", rejected)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
