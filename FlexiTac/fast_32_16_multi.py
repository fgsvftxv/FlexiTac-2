import argparse
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np
import serial

from flexitac_protocol import FrameParser, MAX_PACKET_BYTES


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
RING_LIMIT_BYTES = MAX_PACKET_BYTES * MAX_NODES * 8

PORT = "COM3"
BAUD = 2_000_000

THRESHOLD = 0.5
NOISE_SCALE = 6.0
PITCH_X_MM = 2.5
PITCH_Y_MM = 1.475
ELECTRODE_WIDTH_MM = 0.6
PIXELS_PER_MM = 20
SENSOR_WIDTH_MM = (COLS - 1) * PITCH_X_MM + ELECTRODE_WIDTH_MM
SENSOR_HEIGHT_MM = (ROWS - 1) * PITCH_Y_MM + ELECTRODE_WIDTH_MM
SENSOR_WIDTH_PX = int(np.ceil(SENSOR_WIDTH_MM * PIXELS_PER_MM))
SENSOR_HEIGHT_PX = int(np.ceil(SENSOR_HEIGHT_MM * PIXELS_PER_MM))
HEATMAP_LEFT = 64
HEATMAP_TOP = 48
HEATMAP_WIDTH = HEATMAP_LEFT + SENSOR_WIDTH_PX + 24
HEATMAP_HEIGHT = HEATMAP_TOP + SENSOR_HEIGHT_PX + 64
WINDOW_WIDTH = 1100
WINDOW_HEIGHT = round(WINDOW_WIDTH * HEATMAP_HEIGHT / HEATMAP_WIDTH)


class PhysicalHeatmap:
    def __init__(self):
        self.width_mm = SENSOR_WIDTH_MM
        self.height_mm = SENSOR_HEIGHT_MM
        self.left = HEATMAP_LEFT
        self.top = HEATMAP_TOP
        self.sensor_width_px = SENSOR_WIDTH_PX
        self.sensor_height_px = SENSOR_HEIGHT_PX
        self.background = np.full((HEATMAP_HEIGHT, HEATMAP_WIDTH, 3), 245, dtype=np.uint8)
        half_width = ELECTRODE_WIDTH_MM / 2
        column_edges = [self.left] + [
            self.left + round((half_width + (column - 0.5) * PITCH_X_MM) * PIXELS_PER_MM)
            for column in range(1, COLS)
        ] + [self.left + self.sensor_width_px]
        row_edges = [self.top] + [
            self.top + round((half_width + (row - 0.5) * PITCH_Y_MM) * PIXELS_PER_MM)
            for row in range(1, ROWS)
        ] + [self.top + self.sensor_height_px]
        self.bounds = []
        for row in range(ROWS):
            row_bounds = []
            for column in range(COLS):
                row_bounds.append((column_edges[column], row_edges[row],
                                   column_edges[column + 1], row_edges[row + 1]))
            self.bounds.append(row_bounds)

        ink = (45, 45, 45)
        font = cv2.FONT_HERSHEY_SIMPLEX
        title = (f"{COLS} x {ROWS} | pitch {PITCH_X_MM:g} x {PITCH_Y_MM:g} mm | "
                 "filled cells | relative ADC (not N)")
        cv2.putText(self.background, title, (self.left, 26), font, 0.6, ink, 1, cv2.LINE_AA)
        cv2.putText(self.background, "y / mm", (6, 26), font, 0.45, ink, 1, cv2.LINE_AA)
        axis_bottom = self.top + self.sensor_height_px
        cv2.line(self.background, (self.left - 2, self.top - 1),
                 (self.left - 2, axis_bottom + 1), ink, 1)
        cv2.line(self.background, (self.left - 2, axis_bottom + 1),
                 (self.left + self.sensor_width_px, axis_bottom + 1), ink, 1)
        for position_mm in (0, 10, 20, 30, 40, 50, 60, 70, (COLS - 1) * PITCH_X_MM):
            pixel_x = self.left + round((position_mm + half_width) * PIXELS_PER_MM)
            label = f"{position_mm:g}"
            label_width = cv2.getTextSize(label, font, 0.5, 1)[0][0]
            cv2.line(self.background, (pixel_x, axis_bottom + 1),
                     (pixel_x, axis_bottom + 7), ink, 1)
            cv2.putText(self.background, label, (pixel_x - label_width // 2, axis_bottom + 26),
                        font, 0.5, ink, 1, cv2.LINE_AA)
        for position_mm in (0, 5, 10, 15, 20, (ROWS - 1) * PITCH_Y_MM):
            pixel_y = self.top + round((position_mm + half_width) * PIXELS_PER_MM)
            label = f"{position_mm:g}"
            label_width = cv2.getTextSize(label, font, 0.45, 1)[0][0]
            cv2.line(self.background, (self.left - 8, pixel_y),
                     (self.left - 2, pixel_y), ink, 1)
            cv2.putText(self.background, label, (self.left - 12 - label_width, pixel_y + 5),
                        font, 0.45, ink, 1, cv2.LINE_AA)
        cv2.putText(self.background, "x / mm", (HEATMAP_WIDTH // 2, HEATMAP_HEIGHT - 10),
                    font, 0.5, ink, 1, cv2.LINE_AA)

    def render(self, frame):
        if frame.shape != (ROWS, COLS):
            raise ValueError(f"Expected a {ROWS} x {COLS} normalized frame")
        scaled = np.clip(frame * 255.0, 0, 255).astype(np.uint8)
        colors = cv2.applyColorMap(scaled, cv2.COLORMAP_VIRIDIS)
        image = self.background.copy()
        for row, row_bounds in enumerate(self.bounds):
            for column, (left, top, right, bottom) in enumerate(row_bounds):
                image[top:bottom, left:right] = colors[row, column]
        return image


@dataclass
class ParsedFrame:
    uuid: str
    timestamp_ms: int | None
    data: bytes
    legacy: bool

    @property
    def adc_bits(self):
        return 12 if len(self.data) == FRAME_BYTES * 2 else 8


@dataclass
class NodeState:
    uuid: str
    legacy: bool
    window_name: str
    adc_bits: int = 8
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


def parse_ring(ring, protocol="mini"):
    parser = FrameParser(protocol)
    parser.buffer = ring
    return [ParsedFrame(node, timestamp, payload, protocol == "legacy")
            for node, timestamp, payload in parser.feed(b"")]


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

    contact_data = frame - state.median
    # Keep CLI/display settings in ADC8-equivalent units for both packet formats.
    scale = 16 if state.adc_bits == 12 else 1
    threshold = THRESHOLD * scale
    full_scale = NOISE_SCALE * scale
    contact_data = np.where(contact_data > threshold, contact_data, 0.0)
    state.contact_data_norm = np.clip(contact_data / full_scale, 0.0, 1.0)


def get_or_create_state(parsed):
    global ignored_node_reported

    key = "legacy" if parsed.legacy else parsed.uuid
    state = states.get(key)
    if state is not None:
        if state.adc_bits != parsed.adc_bits:
            # A board was reflashed: rebuild its baseline in the new units.
            state.adc_bits = parsed.adc_bits
            state.median = None
            state.baseline_frames.clear()
            state.ready = False
            state.contact_data_norm.fill(0)
            state.prev_frame.fill(0)
            print(f"ADC format changed for {key}: {parsed.adc_bits} bits; rebuilding baseline")
        return state

    non_legacy_nodes = sum(1 for item in states.values() if not item.legacy)
    if not parsed.legacy and non_legacy_nodes >= MAX_NODES:
        if not ignored_node_reported:
            print(f"Ignore extra nodes: max {MAX_NODES} active nodes")
            ignored_node_reported = True
        return None

    window_name = "Contact Data_left" if parsed.legacy else f"Contact_{parsed.uuid}"
    state = NodeState(uuid=parsed.uuid, legacy=parsed.legacy, window_name=window_name,
                      adc_bits=parsed.adc_bits)
    scale = 16 if state.adc_bits == 12 else 1
    threshold = THRESHOLD * scale
    full_scale = NOISE_SCALE * scale
    print(f"Node {key}: ADC{state.adc_bits}, threshold={threshold:g}, "
          f"full_scale={full_scale:g} ADC counts (CLI scale: {THRESHOLD:g}/{NOISE_SCALE:g})")
    states[key] = state
    return state


def handle_frame(parsed):
    frame = np.frombuffer(parsed.data, dtype="<u2" if parsed.adc_bits == 12 else "u1").reshape((ROWS, COLS)).astype(np.float32)
    now = time.time()

    with states_lock:
        state = get_or_create_state(parsed)
        if state is None:
            return
        update_metrics(state, parsed.timestamp_ms, now)
        preprocess_frame(state, frame)


def serial_reader(ser_dev, protocol="mini"):
    ring = bytearray()
    ser_dev.timeout = 0.01

    while not stop_event.is_set():
        chunk = ser_dev.read(8192)
        if not chunk:
            continue

        ring.extend(chunk)
        if len(ring) > RING_LIMIT_BYTES:
            del ring[:-MAX_PACKET_BYTES]

        for parsed in parse_ring(ring, protocol):
            handle_frame(parsed)


def window_title(state):
    if state.legacy:
        return "Contact Data_left"
    return f"Contact_{state.uuid}_{state.fps:.1f}FPS_Jitter: {state.jitter_ms:.1f}ms"


def create_window_if_needed(state, index):
    if state.window_created:
        return

    cv2.namedWindow(state.window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
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
    heatmap = PhysicalHeatmap()
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

            cv2.imshow(state.window_name, heatmap.render(filtered))

        key = cv2.waitKey(1)
        if key == 27 or key == ord("q"):
            stop_event.set()
            break

        time.sleep(0.005)


def parse_args():
    parser = argparse.ArgumentParser(description="FlexiTac 32x16 multi-node viewer")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help="Baseline difference threshold in ADC8-equivalent display units")
    parser.add_argument("--full-scale", type=float, default=NOISE_SCALE,
                        help="Brightest-color change in ADC8-equivalent display units")
    parser.add_argument("--protocol", choices=("mini", "legacy"), default="mini")
    args = parser.parse_args()
    if not np.isfinite(args.threshold) or args.threshold < 0:
        parser.error("--threshold must be finite and non-negative")
    if not np.isfinite(args.full_scale) or args.full_scale <= 0:
        parser.error("--full-scale must be finite and positive")
    if args.full_scale <= args.threshold:
        parser.error("--full-scale must exceed --threshold")
    return args


def main():
    global THRESHOLD, NOISE_SCALE

    args = parse_args()
    THRESHOLD = args.threshold
    NOISE_SCALE = args.full_scale

    ser_dev = serial.Serial(args.port, args.baud)
    ser_dev.reset_input_buffer()
    print(f"Display threshold={THRESHOLD:g}, full_scale={NOISE_SCALE:g} "
          "ADC8-equivalent units (ADC12 firmware scales these by 16; not calibrated force)")
    print(f"Geometry: {COLS} x {ROWS}, pitch {PITCH_X_MM:g} x {PITCH_Y_MM:g} mm, "
          f"electrode {ELECTRODE_WIDTH_MM:g} mm, "
          f"coverage {SENSOR_WIDTH_MM:g} x {SENSOR_HEIGHT_MM:g} mm")

    serial_thread = threading.Thread(target=serial_reader, args=(ser_dev, args.protocol), daemon=True)
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
