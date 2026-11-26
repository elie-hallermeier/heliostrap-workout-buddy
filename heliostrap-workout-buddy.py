"""
Amazfit Helio – Workout BPM Dashboard
======================================
A PyQt6 GUI that reads live BPM from the Amazfit Helio over BLE and displays
a workout dashboard with colour-coded heart-rate feedback.

Usage:
    python heliostrap-workout-buddy.py                        # auto-discover device
    python heliostrap-workout-buddy.py --address AA:BB:..     # connect directly
"""

import argparse
import asyncio
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QFrame, QProgressBar,
)

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

# ── BLE constants ────────────────────────────────────────────────────────────
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_KEYWORDS = ["helio", "amazfit", "zepp"]

# ── Workout definition ───────────────────────────────────────────────────────
# Each sub-phase of "Cardio surges" is listed individually so the timer is
# always unambiguous.  bpm_low/bpm_high define the "green zone".

@dataclass
class WorkoutPhase:
    name: str
    description: str
    duration_seconds: int
    bpm_low: int
    bpm_high: int


WORKOUT: list[WorkoutPhase] = [
    WorkoutPhase("Warm-up",            "Gradually raise heart rate",        8 * 60,  105, 125),
    WorkoutPhase("Aerobic Block",      "Steady aerobic effort",            10 * 60,  130, 140),
    WorkoutPhase("Recovery",           "Bring heart rate down",             3 * 60,  115, 125),
    WorkoutPhase("Strong Aerobic",     "Controlled effort – not a sprint",  8 * 60,  140, 150),
    WorkoutPhase("Recovery",           "Back down to easy pace",            3 * 60,  115, 125),
    # Cardio surges: 2× (1 min hard + 1 min easy) + 1 min hard = 5 min
    WorkoutPhase("Cardio Surge  ↑",   "Push hard",                         1 * 60,  145, 155),
    WorkoutPhase("Cardio Surge  ↓",   "Active recovery",                   1 * 60,  120, 130),
    WorkoutPhase("Cardio Surge  ↑",   "Push hard",                         1 * 60,  145, 155),
    WorkoutPhase("Cardio Surge  ↓",   "Active recovery",                   1 * 60,  120, 130),
    WorkoutPhase("Cardio Surge  ↑",   "Final push",                        1 * 60,  145, 150),
    WorkoutPhase("Cool-down",          "Slow down gradually",               3 * 60,  105, 115),
]

TOTAL_SECONDS = sum(p.duration_seconds for p in WORKOUT)


def phase_at(elapsed: int) -> tuple[int, WorkoutPhase, int]:
    """Return (phase_index, phase, seconds_remaining_in_phase) for elapsed seconds."""
    acc = 0
    for i, phase in enumerate(WORKOUT):
        acc += phase.duration_seconds
        if elapsed < acc:
            remaining = acc - elapsed
            return i, phase, remaining
    # Workout finished – clamp to last phase
    last = len(WORKOUT) - 1
    return last, WORKOUT[last], 0


# ── Qt signal bridge (thread-safe BPM delivery) ──────────────────────────────

class BleSignals(QObject):
    bpm_received = pyqtSignal(int)
    status_changed = pyqtSignal(str)   # "Scanning…", "Connecting…", "Connected", "Error: …"


# ── Circular BPM widget ──────────────────────────────────────────────────────

class BpmRing(QWidget):
    """Draws a large ring + BPM number in the centre."""

    BLUE  = QColor("#4FC3F7")
    GREEN = QColor("#66BB6A")
    RED   = QColor("#EF5350")
    GREY  = QColor("#546E7A")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bpm: int = 0
        self.color = self.GREY
        self.setMinimumSize(260, 260)

    def set_bpm(self, bpm: int, color: QColor) -> None:
        self.bpm = bpm
        self.color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        size = min(w, h) - 20
        x = (w - size) // 2
        y = (h - size) // 2

        # Background circle
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#1E2A35")))
        painter.drawEllipse(x, y, size, size)

        # Coloured ring
        ring_pen = QPen(self.color, 14)
        ring_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        margin = 10
        painter.drawEllipse(x + margin, y + margin, size - 2 * margin, size - 2 * margin)

        # BPM number
        painter.setPen(QPen(self.color))
        font = QFont("SF Pro Display", int(size // 3.5), QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -2)
        painter.setFont(font)
        label = str(self.bpm) if self.bpm else "---"
        painter.drawText(x, y, size, size, Qt.AlignmentFlag.AlignCenter, label)

        # "BPM" sub-label
        painter.setPen(QPen(QColor("#90A4AE")))
        sub_font = QFont("SF Pro Display", size // 14)
        painter.setFont(sub_font)
        painter.drawText(x, y + size // 2 + size // 8, size, size // 8,
                         Qt.AlignmentFlag.AlignCenter, "BPM")

        painter.end()


# ── Main window ──────────────────────────────────────────────────────────────

class Dashboard(QMainWindow):

    def __init__(self, ble_signals: BleSignals) -> None:
        super().__init__()
        self.ble_signals = ble_signals
        self.elapsed_seconds = 0
        self.current_bpm = 0
        self.workout_started = False

        self.setWindowTitle("Helio Workout Dashboard")
        self.setMinimumSize(520, 640)
        self._apply_dark_palette()
        self._build_ui()

        # 1-second tick for timers
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._on_tick)
        self.tick_timer.start(1000)

        ble_signals.bpm_received.connect(self._on_bpm)
        ble_signals.status_changed.connect(self._on_status)

    # ── UI construction ──────────────────────────────────────────────────────

    def _apply_dark_palette(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #121C24;
                color: #ECEFF1;
                font-family: 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif;
            }
            QProgressBar {
                border: none;
                border-radius: 5px;
                background-color: #1E2A35;
                height: 10px;
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 5px;
                background-color: #4FC3F7;
            }
            QFrame#card {
                background-color: #1A2633;
                border-radius: 16px;
            }
        """)

    def _label(self, text: str, size: int, bold: bool = False,
               color: str = "#ECEFF1", align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignCenter) -> QLabel:
        lbl = QLabel(text)
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        lbl.setFont(QFont("SF Pro Display", size, weight))
        lbl.setStyleSheet(f"color: {color};")
        lbl.setAlignment(align)
        return lbl

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(16)

        # ── Top bar: title + total timer ────────────────────────────────────
        top_bar = QHBoxLayout()
        title = self._label("❤  HELIO WORKOUT", 26, bold=True,
                            color="#4FC3F7", align=Qt.AlignmentFlag.AlignLeft)
        self.total_timer_label = self._label("00:00", 26, bold=True,
                                             color="#90A4AE",
                                             align=Qt.AlignmentFlag.AlignRight)
        top_bar.addWidget(title)
        top_bar.addStretch()
        top_bar.addWidget(self.total_timer_label)
        outer.addLayout(top_bar)

        # ── Phase card ───────────────────────────────────────────────────────
        phase_card = QFrame()
        phase_card.setObjectName("card")
        phase_layout = QVBoxLayout(phase_card)
        phase_layout.setContentsMargins(20, 16, 20, 16)
        phase_layout.setSpacing(6)

        self.phase_number_label = self._label("PHASE 1 / 11", 18, color="#607D8B")
        self.phase_name_label   = self._label("Warm-up", 44, bold=True, color="#ECEFF1")
        self.phase_desc_label   = self._label("Gradually raise heart rate", 20, color="#90A4AE")
        self.phase_range_label  = self._label("Target: 105 – 125 bpm", 22, bold=True, color="#4FC3F7")

        phase_layout.addWidget(self.phase_number_label)
        phase_layout.addWidget(self.phase_name_label)
        phase_layout.addWidget(self.phase_desc_label)
        phase_layout.addWidget(self.phase_range_label)

        outer.addWidget(phase_card)

        # ── BPM ring ─────────────────────────────────────────────────────────
        self.bpm_ring = BpmRing()
        outer.addWidget(self.bpm_ring, alignment=Qt.AlignmentFlag.AlignHCenter)

        # ── Phase countdown ──────────────────────────────────────────────────
        self.phase_countdown_label = self._label("8:00 left in phase", 28, bold=True, color="#90A4AE")
        outer.addWidget(self.phase_countdown_label)

        # ── Phase progress bar ───────────────────────────────────────────────
        self.phase_progress = QProgressBar()
        self.phase_progress.setRange(0, 100)
        self.phase_progress.setValue(0)
        self.phase_progress.setTextVisible(False)
        self.phase_progress.setFixedHeight(10)
        outer.addWidget(self.phase_progress)

        # ── Workout overview row ─────────────────────────────────────────────
        overview_card = QFrame()
        overview_card.setObjectName("card")
        ov_layout = QHBoxLayout(overview_card)
        ov_layout.setContentsMargins(20, 12, 20, 12)

        total_str = f"Total: {TOTAL_SECONDS // 60} min"
        self.overview_total = self._label(total_str, 18, color="#607D8B")
        self.overview_remaining = self._label("Remaining: 40:00", 18, color="#607D8B")
        self.status_label = self._label("Connecting…", 18, color="#FFA726")

        ov_layout.addWidget(self.overview_total)
        ov_layout.addStretch()
        ov_layout.addWidget(self.status_label)
        ov_layout.addStretch()
        ov_layout.addWidget(self.overview_remaining)

        outer.addWidget(overview_card)

        # Initialise display with phase 0 data
        self._refresh_phase_ui(0, WORKOUT[0], WORKOUT[0].duration_seconds)

    # ── UI refresh helpers ───────────────────────────────────────────────────

    def _refresh_phase_ui(self, idx: int, phase: WorkoutPhase, remaining: int) -> None:
        self.phase_number_label.setText(f"PHASE {idx + 1} / {len(WORKOUT)}")
        self.phase_name_label.setText(phase.name)
        self.phase_desc_label.setText(phase.description)
        self.phase_range_label.setText(f"Target: {phase.bpm_low} – {phase.bpm_high} bpm")

        mins, secs = divmod(remaining, 60)
        self.phase_countdown_label.setText(f"{mins}:{secs:02d} left in phase")

        spent = phase.duration_seconds - remaining
        pct = int(spent / phase.duration_seconds * 100) if phase.duration_seconds else 100
        self.phase_progress.setValue(pct)

        # Update progress bar colour based on phase type
        name_lower = phase.name.lower()
        if "recovery" in name_lower or "cool" in name_lower:
            chunk_color = "#66BB6A"
        elif "surge" in name_lower and "↑" in phase.name:
            chunk_color = "#EF5350"
        elif "aerobic" in name_lower:
            chunk_color = "#FFA726"
        else:
            chunk_color = "#4FC3F7"
        self.phase_progress.setStyleSheet(
            f"QProgressBar::chunk {{ border-radius: 5px; background-color: {chunk_color}; }}"
        )

    def _bpm_color(self, bpm: int, phase: WorkoutPhase) -> QColor:
        if bpm == 0:
            return BpmRing.GREY
        if bpm < phase.bpm_low:
            return BpmRing.BLUE
        if bpm > phase.bpm_high:
            return BpmRing.RED
        return BpmRing.GREEN

    # ── Slots ────────────────────────────────────────────────────────────────

    def _on_bpm(self, bpm: int) -> None:
        self.current_bpm = bpm
        if not self.workout_started:
            self.workout_started = True

        idx, phase, remaining = phase_at(self.elapsed_seconds)
        color = self._bpm_color(bpm, phase)
        self.bpm_ring.set_bpm(bpm, color)

    def _on_status(self, status: str) -> None:
        self.status_label.setText(status)
        if "connected" in status.lower():
            self.status_label.setStyleSheet("color: #66BB6A;")
        elif "error" in status.lower() or "failed" in status.lower():
            self.status_label.setStyleSheet("color: #EF5350;")
        else:
            self.status_label.setStyleSheet("color: #FFA726;")

    def _on_tick(self) -> None:
        if self.workout_started:
            self.elapsed_seconds += 1

        # Total elapsed timer (top right)
        total_mins, total_secs = divmod(self.elapsed_seconds, 60)
        self.total_timer_label.setText(f"{total_mins:02d}:{total_secs:02d}")

        # Phase info
        idx, phase, remaining = phase_at(self.elapsed_seconds)
        self._refresh_phase_ui(idx, phase, remaining)

        # Workout remaining
        workout_remaining = max(0, TOTAL_SECONDS - self.elapsed_seconds)
        wr_mins, wr_secs = divmod(workout_remaining, 60)
        self.overview_remaining.setText(f"Remaining: {wr_mins}:{wr_secs:02d}")

        # Keep BPM ring colour current even if no new reading came in
        if self.current_bpm:
            color = self._bpm_color(self.current_bpm, phase)
            self.bpm_ring.set_bpm(self.current_bpm, color)


# ── BLE background thread ────────────────────────────────────────────────────

def parse_heart_rate(data: bytearray) -> int:
    flags = data[0]
    if flags & 0x01:
        return struct.unpack_from("<H", data, 1)[0]
    return struct.unpack_from("<B", data, 1)[0]


async def ble_loop(address: Optional[str], signals: BleSignals) -> None:
    if address:
        target = address
        signals.status_changed.emit("Connecting…")
    else:
        signals.status_changed.emit("Scanning…")
        devices = await BleakScanner.discover(timeout=12.0)
        match = next(
            (d for d in devices if any(k in (d.name or "").lower() for k in DEVICE_NAME_KEYWORDS)),
            None,
        )
        if match is None:
            signals.status_changed.emit("Error: device not found")
            return
        target = match.address
        signals.status_changed.emit(f"Found {match.name} – connecting…")

    def handler(char: BleakGATTCharacteristic, data: bytearray) -> None:
        signals.bpm_received.emit(parse_heart_rate(data))

    try:
        async with BleakClient(target) as client:
            if not client.is_connected:
                signals.status_changed.emit("Error: failed to connect")
                return
            signals.status_changed.emit("Connected ✓")
            await client.start_notify(HEART_RATE_MEASUREMENT_UUID, handler)
            while client.is_connected:
                await asyncio.sleep(1)
            signals.status_changed.emit("Disconnected")
    except Exception as exc:
        signals.status_changed.emit(f"Error: {exc}")


def start_ble_thread(address: Optional[str], signals: BleSignals) -> None:
    """Run the asyncio BLE loop in a daemon thread so Qt owns the main thread."""
    def run() -> None:
        asyncio.run(ble_loop(address, signals))

    t = threading.Thread(target=run, daemon=True)
    t.start()


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Amazfit Helio workout BPM dashboard.")
    parser.add_argument(
        "--address",
        type=str,
        default=None,
        help="Bluetooth address (e.g. AA:BB:CC:DD:EE:FF). Omit to auto-discover.",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Helio Dashboard")

    signals = BleSignals()
    window = Dashboard(signals)
    window.show()

    start_ble_thread(args.address, signals)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()



