"""Offline protocol tests; no physical serial device is opened."""

import unittest

from flexitac_protocol import FrameParser, crc16


def packet(node, timestamp, payload):
    data = (b"\x54\xaa" + node.to_bytes(2, "little")
            + timestamp.to_bytes(4, "little") + len(payload).to_bytes(2, "little")
            + b"\xaa\x55" + payload)
    return data + crc16(data).to_bytes(2, "little")


class CaptureProtocolTests(unittest.TestCase):
    def test_known_crc_vector(self):
        self.assertEqual(crc16(b"123456789"), 0x4B37)

    def test_fragmented_multinode_values_and_timestamp_are_unchanged(self):
        payload = bytes(range(256)) * 2
        stream = (b"startup" + packet(0xC086, 0xFFFFFFFF, payload)
                  + packet(0xC097, 0, payload[::-1]))
        parser = FrameParser()
        frames = []
        for start in range(0, len(stream), 7):
            frames.extend(parser.feed(stream[start:start + 7]))
        self.assertEqual(frames, [("C086", 0xFFFFFFFF, payload), ("C097", 0, payload[::-1])])
        self.assertEqual(parser.discarded_bytes, len(b"startup"))
        self.assertEqual(parser.bad_crc, 0)

    def test_adc12_packet_preserves_little_endian_counts_above_255(self):
        values = [0, 15, 160, 176, 2048, 4095] + [0] * (512 - 6)
        payload = b"".join(value.to_bytes(2, "little") for value in values)
        parser = FrameParser()
        frames = list(parser.feed(packet(0xC086, 123, payload)))
        self.assertEqual(frames, [("C086", 123, payload)])
        decoded = [int.from_bytes(payload[i:i + 2], "little")
                   for i in range(0, len(payload), 2)]
        self.assertEqual(decoded[:6], values[:6])

    def test_parser_accepts_old_adc8_and_new_adc12_packets_in_one_stream(self):
        old = bytes([10]) * 512
        new = b"\xff\x0f" * 512
        parser = FrameParser()
        frames = list(parser.feed(packet(1, 1, old) + packet(2, 2, new)))
        self.assertEqual([len(frame[2]) for frame in frames], [512, 1024])

    def test_corruption_and_deleted_byte_recover_without_legacy_false_frame(self):
        payload = b"\xaa\x55" * 256
        bad = bytearray(packet(1, 10, payload))
        bad[30] ^= 1
        deleted = packet(1, 20, payload)
        deleted = deleted[:33] + deleted[34:]
        good = packet(2, 30, bytes(512))
        parser = FrameParser()
        self.assertEqual(list(parser.feed(bytes(bad) + deleted + good)),
                         [("0002", 30, bytes(512))])
        self.assertGreaterEqual(parser.bad_crc, 2)

    def test_partial_trailing_frame_retained(self):
        parser = FrameParser()
        data = packet(1, 10, bytes(512))
        self.assertEqual(list(parser.feed(data[:200])), [])
        self.assertEqual(bytes(parser.buffer), data[:200])
        self.assertEqual(list(parser.feed(data[200:])), [("0001", 10, bytes(512))])

    def test_legacy_requires_explicit_selection(self):
        payload = bytes(range(256)) * 2
        parser = FrameParser("legacy")
        self.assertEqual(list(parser.feed(b"\xaa\x55" + payload)),
                         [("legacy", None, payload)])


if __name__ == "__main__":
    unittest.main()
