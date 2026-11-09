import argparse
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np
import serial


ROWS, COLS = 16, 32
FRAME_BYTES = ROWS * COLS

LEGACY_MAGIC = b"\xAA\x55"
EXT_MAGIC = b"\x54\xAA"

UUID_OFFSET = 2
TIMESTAMP_OFFSET = 4
DATA_SIZE_OFFSET = 8
DATA_MAGIC_OFFSET = 10
DATA_OFFSET = 12
CRC_OFFSET = DATA_OFFSET + FRAME_BYTES
PACKET_BYTES = CRC_OFFSET + 2

MAX_NODES = 16
INIT_FRAMES = 30
METRIC_WINDOW_SECONDS = 0.5
RING_LIMIT_BYTES = PACKET_BYTES * MAX_NODES * 8

PORT = "COM3"
BAUD = 2_000_000

THRESHOLD = 15
NOISE_SCALE = 60
WINDOW_WIDTH = COLS * 30
WINDOW_HEIGHT = ROWS * 30


@dataclass
class ParsedFrame:
    uuid: str
    timestamp_ms: int | None
    data: bytes
    legacy: bool


@dataclass
class NodeState:
    uuid: str
    legacy: bool
    window_name: str
    baseline_frames: list[np.ndarray] = field(default_factory=list)
    median: np.ndarray | None = None
    contact_data_norm: np.ndarray = field(
        default_factory=lambda: np.zeros((ROWS, COLS), dtype=np.float32)
    )
    prev_frame: np.ndarray = field(
        default_factory=lambda: np.zeros((ROWS, COLS), dtype=np.float32)
    )
    receive_times: deque[float] = field(default_factory=deque)
    timestamp_deltas: deque[tuple[float, int]] = field(default_factory=deque)
    last_timestamp_ms: int | None = None
    fps: float = 0.0
    jitter_ms: float = 0.0
    ready: bool = False
    window_created: bool = False
    title_update_time: float = 0.0


states: dict[str, NodeState] = {}
states_lock = threading.Lock()
stop_event = threading.Event()
ignored_node_reported = False


def temporal_filter(new_frame, prev_frame, alpha=0.2):
    return alpha * new_frame + (1 - alpha) * prev_frame


def crc16_modbus(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def parse_extended_packet(packet):
    if packet[:2] != EXT_MAGIC:
        return None
    if packet[DATA_MAGIC_OFFSET:DATA_OFFSET] != LEGACY_MAGIC:
        return None

    data_size = int.from_bytes(packet[DATA_SIZE_OFFSET:DATA_MAGIC_OFFSET], "little")
    if data_size != FRAME_BYTES:
        return None

    expected_crc = int.from_bytes(packet[CRC_OFFSET:PACKET_BYTES], "little")
    actual_crc = crc16_modbus(packet[:CRC_OFFSET])
    if actual_crc != expected_crc:
        return None

    uuid = int.from_bytes(packet[UUID_OFFSET:TIMESTAMP_OFFSET], "little")
    timestamp_ms = int.from_bytes(packet[TIMESTAMP_OFFSET:DATA_SIZE_OFFSET], "little")
    return ParsedFrame(
        uuid=f"{uuid:04X}",
        timestamp_ms=timestamp_ms,
        data=bytes(packet[DATA_OFFSET:CRC_OFFSET]),
        legacy=False,
    )


def find_next_magic(ring):
    ext_idx = ring.find(EXT_MAGIC)
    legacy_idx = ring.find(LEGACY_MAGIC)

    if ext_idx < 0 and legacy_idx < 0:
        return None, -1
    if ext_idx >= 0 and (legacy_idx < 0 or ext_idx <= legacy_idx):
        return "extended", ext_idx
    return "legacy", legacy_idx


def parse_ring(ring):
    frames = []

    while True:
        kind, idx = find_next_magic(ring)
        if kind is None:
            if len(ring) > 1:
                del ring[:-1]
            break

        if idx > 0:
            del ring[:idx]

        if kind == "extended":
            if len(ring) >= DATA_OFFSET:
                data_size = int.from_bytes(
                    ring[DATA_SIZE_OFFSET:DATA_MAGIC_OFFSET], "little"
                )
                if data_size != FRAME_BYTES or ring[DATA_MAGIC_OFFSET:DATA_OFFSET] != LEGACY_MAGIC:
                    del ring[0]
                    continue

            if len(ring) < PACKET_BYTES:
                break

            parsed = parse_extended_packet(bytes(ring[:PACKET_BYTES]))
            if parsed is None:
                del ring[:PACKET_BYTES]
                continue

            frames.append(parsed)
            del ring[:PACKET_BYTES]
            continue

        if len(ring) < len(LEGACY_MAGIC) + FRAME_BYTES:
            break

        frames.append(
            ParsedFrame(
                uuid="legacy",
                timestamp_ms=None,
                data=bytes(ring[len(LEGACY_MAGIC):len(LEGACY_MAGIC) + FRAME_BYTES]),
                legacy=True,
            )
        )
        del ring[:len(LEGACY_MAGIC) + FRAME_BYTES]

    return frames


def prune_metrics(state, now):
    cutoff = now - METRIC_WINDOW_SECONDS
    while state.receive_times and state.receive_times[0] < cutoff:
        state.receive_times.popleft()
    while state.timestamp_deltas and state.timestamp_deltas[0][0] < cutoff:
        state.timestamp_deltas.popleft()


def update_metrics(state, timestamp_ms, now):
    state.receive_times.append(now)

    if timestamp_ms is not None:
        if state.last_timestamp_ms is not None:
            delta_ms = (timestamp_ms - state.last_timestamp_ms) & 0xFFFFFFFF
            if delta_ms <= 5000:
                state.timestamp_deltas.append((now, delta_ms))
        state.last_timestamp_ms = timestamp_ms

    prune_metrics(state, now)

    if len(state.receive_times) >= 2:
        span = state.receive_times[-1] - state.receive_times[0]
        state.fps = (len(state.receive_times) - 1) / span if span > 0 else 0.0
    else:
        state.fps = 0.0

    if state.timestamp_deltas:
        state.jitter_ms = sum(delta for _, delta in state.timestamp_deltas) / len(
            state.timestamp_deltas
        )
    else:
        state.jitter_ms = 0.0


def preprocess_frame(state, frame):
    if state.median is None:
        state.baseline_frames.append(frame)
        if len(state.baseline_frames) >= INIT_FRAMES:
            state.median = np.median(np.stack(state.baseline_frames, axis=0), axis=0)
            state.baseline_frames.clear()
            state.ready = True
            print(f"Finish Initialization: {state.uuid}")
        return

    contact_data = frame - state.median - THRESHOLD
    contact_data = np.clip(contact_data, 0, 100)

    max_value = np.max(contact_data)
    if max_value < THRESHOLD:
        state.contact_data_norm = contact_data / NOISE_SCALE
    else:
        state.contact_data_norm = contact_data / (max_value + 1e-6)


def get_or_create_state(parsed):
    global ignored_node_reported

    key = "legacy" if parsed.legacy else parsed.uuid
    state = states.get(key)
    if state is not None:
        return state

    non_legacy_nodes = sum(1 for item in states.values() if not item.legacy)
    if not parsed.legacy and non_legacy_nodes >= MAX_NODES:
        if not ignored_node_reported:
            print(f"Ignore extra nodes: max {MAX_NODES} active nodes")
            ignored_node_reported = True
        return None

    window_name = "Contact Data_left" if parsed.legacy else f"Contact_{parsed.uuid}"
    state = NodeState(uuid=parsed.uuid, legacy=parsed.legacy, window_name=window_name)
    states[key] = state
    return state


def handle_frame(parsed):
    frame = np.frombuffer(parsed.data, dtype=np.uint8).reshape((ROWS, COLS)).astype(np.float32)
    now = time.time()

    with states_lock:
        state = get_or_create_state(parsed)
        if state is None:
            return
        update_metrics(state, parsed.timestamp_ms, now)
        preprocess_frame(state, frame)


def serial_reader(ser_dev):
    ring = bytearray()
    ser_dev.timeout = 0.01

    while not stop_event.is_set():
        chunk = ser_dev.read(8192)
        if not chunk:
            continue

        ring.extend(chunk)
        if len(ring) > RING_LIMIT_BYTES:
            del ring[:-PACKET_BYTES]

        for parsed in parse_ring(ring):
            handle_frame(parsed)


def window_title(state):
    if state.legacy:
        return "Contact Data_left"
    return f"Contact_{state.uuid}_{state.fps:.1f}FPS_Jitter: {state.jitter_ms:.1f}ms"


def create_window_if_needed(state, index):
    if state.window_created:
        return

    cv2.namedWindow(state.window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(state.window_name, WINDOW_WIDTH, WINDOW_HEIGHT)
    cv2.moveWindow(
        state.window_name,
        (index % 4) * (WINDOW_WIDTH + 24),
        (index // 4) * (WINDOW_HEIGHT + 72),
    )
    state.window_created = True


def set_window_title(window_name, title):
    if not hasattr(cv2, "setWindowTitle"):
        return
    try:
        cv2.setWindowTitle(window_name, title)
    except cv2.error:
        pass


def display_loop():
    while not stop_event.is_set():
        with states_lock:
            active_states = [state for state in states.values() if state.ready]

        for index, state in enumerate(active_states):
            with states_lock:
                create_window_if_needed(state, index)
                title = window_title(state)
                if time.time() - state.title_update_time >= 0.1:
                    set_window_title(state.window_name, title)
                    state.title_update_time = time.time()

                filtered = temporal_filter(state.contact_data_norm, state.prev_frame, alpha=0.2)
                state.prev_frame = filtered

            scaled = np.clip(filtered * 255.0, 0, 255).astype(np.uint8)
            colormap = cv2.applyColorMap(scaled, cv2.COLORMAP_VIRIDIS)
            cv2.imshow(state.window_name, colormap)

        key = cv2.waitKey(1)
        if key == 27 or key == ord("q"):
            stop_event.set()
            break

        time.sleep(0.005)


def parse_args():
    parser = argparse.ArgumentParser(description="FlexiTac 32x16 multi-node viewer")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baud", type=int, default=BAUD)
    return parser.parse_args()


def main():
    args = parse_args()

    ser_dev = serial.Serial(args.port, args.baud)
    ser_dev.flush()
    ser_dev.reset_input_buffer()

    serial_thread = threading.Thread(target=serial_reader, args=(ser_dev,), daemon=True)
    serial_thread.start()

    print("receive data test")
    try:
        display_loop()
    finally:
        stop_event.set()
        serial_thread.join(timeout=1.0)
        ser_dev.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
