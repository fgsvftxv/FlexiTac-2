"""Mini ADC8/ADC12 packet parsing; never reinterpret corrupt Mini data as legacy."""

ROWS, COLS = 16, 32
SAMPLE_COUNT = ROWS * COLS
EXT_MAGIC = b"\x54\xaa"
LEGACY_MAGIC = b"\xaa\x55"
DATA_OFFSET = 12
PAYLOAD_BYTES = (SAMPLE_COUNT, SAMPLE_COUNT * 2)
MAX_PACKET_BYTES = DATA_OFFSET + max(PAYLOAD_BYTES) + 2


def crc16(data):
    """CRC-16/MODBUS, matching the firmware."""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


class FrameParser:
    def __init__(self, protocol="mini"):
        if protocol not in ("mini", "legacy"):
            raise ValueError("protocol must be mini or legacy")
        self.protocol = protocol
        self.buffer = bytearray()
        self.bad_crc = 0
        self.bad_header = 0
        self.discarded_bytes = 0

    def feed(self, chunk):
        self.buffer.extend(chunk)
        magic = EXT_MAGIC if self.protocol == "mini" else LEGACY_MAGIC
        while True:
            index = self.buffer.find(magic)
            if index < 0:
                keep = int(self.buffer[-1:] == magic[:1])
                drop = len(self.buffer) - keep
                self.discarded_bytes += drop
                if drop:
                    del self.buffer[:drop]
                return
            self.discarded_bytes += index
            del self.buffer[:index]
            if self.protocol == "mini":
                if len(self.buffer) < DATA_OFFSET:
                    return
                payload_bytes = int.from_bytes(self.buffer[8:10], "little")
                if (payload_bytes not in PAYLOAD_BYTES
                        or self.buffer[10:12] != LEGACY_MAGIC):
                    self.bad_header += 1
                    self.discarded_bytes += 1
                    del self.buffer[0]
                    continue
                size = DATA_OFFSET + payload_bytes + 2
            else:
                size = SAMPLE_COUNT + len(LEGACY_MAGIC)
            if len(self.buffer) < size:
                return
            packet = bytes(self.buffer[:size])
            if self.protocol == "mini":
                if crc16(packet[:-2]) != int.from_bytes(packet[-2:], "little"):
                    self.bad_crc += 1
                    self.discarded_bytes += 1
                    del self.buffer[0]
                    continue
                node = f"{int.from_bytes(packet[2:4], 'little'):04X}"
                timestamp_ms = int.from_bytes(packet[4:8], "little")
                payload = packet[DATA_OFFSET:-2]
            else:
                node, timestamp_ms, payload = "legacy", None, packet[2:]
            del self.buffer[:size]
            yield node, timestamp_ms, payload
