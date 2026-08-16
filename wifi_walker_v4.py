#!/usr/bin/env python3
"""
Wi-Fi Walker v10

Core model:
    1 percentage point of the RSSI-derived model = 0.1 meter.

Verification:
    - Every meaningful RSSI-model change becomes an observation.
    - Observations are FIFO.
    - Every 3 observations form a verification batch.
    - New observations arriving while a batch is being handled remain
      in the FIFO for the next batch.
    - A batch is NOT rejected merely because one of the three readings
      briefly moves in the opposite direction.
    - Verification uses the NET signed change across the batch, which
      is much more appropriate for walking than summing every tiny
      absolute fluctuation.

Scanner:
    - Never overlaps scans.
    - Resource-busy gets adaptive retry/backoff.
    - The scanner runs as fast as the Wi-Fi driver actually permits.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import threading
import queue
import time
import re
import shutil
import json
from collections import deque


# ============================================================
# MODEL
# ============================================================

METERS_PER_PERCENT = 0.10

# Scan is adaptive: this is only a tiny yield after a successful scan.
SCAN_YIELD = 1

SCAN_TIMEOUT = 10.0

# Fast response to RSSI changes.
RSSI_ALPHA = 0.15
# 1% = 0.1m.
MIN_PERCENT_CHANGE = 1.0

# Exactly 3 observations per verification.
VERIFY_SIZE = 3
# How much of a batch's total movement must survive as NET movement.
#
# Example:
#   +1%, +2%, +1% => net +4% => accepted
#
# A brief reversal is allowed:
#   +3%, -1%, +3% => net +5% => accepted
#
# But pure noise:
#   +1%, -1%, +1% => net +1% => rejected
MIN_NET_PERCENT = 2.0

# Maximum single observation.
MAX_OBSERVATION_PERCENT = 50.0

# Queue is deliberately large.
MAX_VERIFICATION_QUEUE = 2000

RSSI_NEAR_DBM = -35.0
RSSI_FAR_DBM = -85.0
MIN_USABLE_RSSI = -88.0

# Do not switch APs just because another AP is 1 dB stronger.
AP_SWITCH_MARGIN_DB = 5.0


# ============================================================
# HELPERS
# ============================================================

def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def rssi_to_percent(rssi):
    return clamp(
        (rssi - RSSI_FAR_DBM)
        / (RSSI_NEAR_DBM - RSSI_FAR_DBM)
        * 100.0,
        0.0,
        100.0
    )


def percent_to_meters(percent):
    return abs(percent) * METERS_PER_PERCENT


# ============================================================
# WIFI INTERFACE
# ============================================================

def find_wifi_interface():
    if not shutil.which("iw"):
        return None

    try:
        result = subprocess.run(
            ["iw", "dev"],
            capture_output=True,
            text=True,
            timeout=2
        )

        for line in result.stdout.splitlines():
            line = line.strip()

            if line.startswith("Interface "):
                return line.split()[1]

    except Exception:
        pass

    return None


def scan_wifi(interface):
    """
    Perform exactly ONE scan.

    This function never launches another scan while this one is running.
    Resource-busy handling belongs to scanner_worker().
    """
    try:
        result = subprocess.run(
            [
                "sudo", "-n",
                "iw", "dev", interface, "scan"
            ],
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT
        )

        stderr = result.stderr.strip()

        if result.returncode != 0:

            if "busy" in stderr.lower():
                return None, "BUSY"

            return None, stderr or "scan failed"

        networks = {}

        bssid = None
        freq = None
        ssid = ""

        for line in result.stdout.splitlines():

            match = re.match(
                r"\s*BSS\s+([0-9a-fA-F:]{17})",
                line
            )

            if match:
                bssid = match.group(1).lower()
                freq = None
                ssid = ""
                continue

            if bssid is None:
                continue

            match = re.search(
                r"\s*freq:\s*(\d+(?:\.\d+)?)",
                line
            )

            if match:
                freq = float(match.group(1))

            match = re.search(
                r"\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm",
                line
            )

            if match:
                networks[bssid] = {
                    "rssi": float(match.group(1)),
                    "freq": freq,
                    "ssid": ssid
                }

            match = re.match(
                r"\s*SSID:\s?(.*)",
                line
            )

            if match:
                ssid = match.group(1)

                if bssid in networks:
                    networks[bssid]["ssid"] = ssid

        return networks, None

    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"

    except Exception as e:
        return None, str(e)


# ============================================================
# AP TRACKER
# ============================================================

class APTracker:

    def __init__(self, bssid):

        self.bssid = bssid

        self.filtered_rssi = None
        self.previous_percent = None

        # This is the IMPORTANT queue.
        #
        # Every detected observation goes here.
        # Verification consumes ONLY the oldest three.
        #
        # If observations 4,5,6 arrive while 1,2,3 are being
        # verified, 4,5,6 stay here untouched.
        self.observation_queue = deque(
            maxlen=MAX_VERIFICATION_QUEUE
        )

        self.observations_seen = 0
        self.verified_batches = 0
        self.rejected_batches = 0

        self.last_verified_movement = 0.0

    def update(self, rssi):

        # First reading establishes the baseline.
        if self.filtered_rssi is None:

            self.filtered_rssi = rssi

            self.previous_percent = (
                rssi_to_percent(rssi)
            )

            return self.result()

        old_filtered = self.filtered_rssi

        self.filtered_rssi = (
            RSSI_ALPHA * rssi
            +
            (1.0 - RSSI_ALPHA) * old_filtered
        )

        current_percent = rssi_to_percent(
            self.filtered_rssi
        )

        delta_percent = (
            current_percent
            -
            self.previous_percent
        )

        self.previous_percent = current_percent

        # Ignore sub-1% changes.
        if abs(delta_percent) < MIN_PERCENT_CHANGE:
            return self.result()

        delta_percent = clamp(
            delta_percent,
            -MAX_OBSERVATION_PERCENT,
            MAX_OBSERVATION_PERCENT
        )

        self.observation_queue.append({
            "delta_percent": delta_percent,
            "meters": percent_to_meters(delta_percent),
            "percent": current_percent,
            "rssi": self.filtered_rssi,
            "time": time.time()
        })

        self.observations_seen += 1

        committed, batch, rejected = (
            self.process_batches()
        )

        return self.result(
            candidate=percent_to_meters(
                delta_percent
            ),
            committed=committed,
            batch=batch,
            rejected=rejected
        )

    def process_batches(self):

        committed = 0.0
        last_batch = []
        rejected = False

        # Drain every COMPLETE batch that is currently available.
        while len(self.observation_queue) >= VERIFY_SIZE:

            # ------------------------------------------------
            # FIFO SNAPSHOT
            # ------------------------------------------------
            #
            # Only these three observations belong to this
            # verification.
            #
            # Anything that arrives later remains queued.
            # ------------------------------------------------

            batch = [
                self.observation_queue.popleft()
                for _ in range(VERIFY_SIZE)
            ]

            last_batch = batch

            deltas = [
                x["delta_percent"]
                for x in batch
            ]

            # ------------------------------------------------
            # KEY FIX FROM v9
            # ------------------------------------------------
            #
            # Do NOT require all 3 observations to have the
            # same direction.
            #
            # Wi-Fi RSSI is noisy. A person can walk forward
            # while one scan fluctuates backward by 1-2%.
            #
            # Instead, calculate NET movement:
            #
            #     +3% - 1% + 3% = +5%
            #
            # That is still clearly forward movement.
            # ------------------------------------------------

            net_percent = sum(deltas)

            # Tiny net change = likely radio noise.
            if abs(net_percent) < MIN_NET_PERCENT:

                self.rejected_batches += 1
                rejected = True

                continue

            # Convert NET movement, not absolute fluctuation,
            # into committed distance.
            net_meters = percent_to_meters(
                net_percent
            )

            committed += net_meters

            self.verified_batches += 1

            self.last_verified_movement = net_meters

        return (
            committed,
            last_batch,
            rejected
        )

    def result(
        self,
        candidate=0.0,
        committed=0.0,
        batch=None,
        rejected=False
    ):

        return {
            "candidate": candidate,
            "committed": committed,

            "percent": (
                self.previous_percent or 0.0
            ),

            "queue_size": len(
                self.observation_queue
            ),

            "verified": committed > 0,

            "rejected": rejected,

            "batch": batch or []
        }


# ============================================================
# MOVEMENT ENGINE
# ============================================================

class MovementEngine:

    def __init__(self):

        self.trackers = {}

        self.active_bssid = None

        self.total_distance = 0.0

        self.speed = 4

        self.last_commit_time = None

        self.events = 0

        self.lock = threading.Lock()

    def choose_ap(self, networks):

        usable = [
            (bssid, data)
            for bssid, data in networks.items()
            if data["rssi"] >= MIN_USABLE_RSSI
        ]

        if not usable:
            return None

        strongest_bssid, strongest_data = max(
            usable,
            key=lambda x: x[1]["rssi"]
        )

        if self.active_bssid is None:
            return strongest_bssid

        if self.active_bssid not in networks:
            return strongest_bssid

        current_rssi = networks[
            self.active_bssid
        ]["rssi"]

        # Keep tracking the current AP unless another AP
        # becomes clearly stronger.
        if (
            strongest_bssid != self.active_bssid
            and
            strongest_data["rssi"]
            >
            current_rssi + AP_SWITCH_MARGIN_DB
        ):
            return strongest_bssid

        return self.active_bssid

    def update(self, networks):

        with self.lock:

            now = time.time()

            bssid = self.choose_ap(
                networks
            )

            if bssid is None:

                return {
                    "candidate": 0.0,
                    "committed": 0.0,
                    "queue_size": 0,
                    "percent": 0.0,
                    "active_ap": None,
                    "verified": False,
                    "rejected": False,
                    "batch": []
                }

            if bssid not in self.trackers:

                self.trackers[bssid] = (
                    APTracker(bssid)
                )

            self.active_bssid = bssid

            tracker = self.trackers[bssid]

            result = tracker.update(
                networks[bssid]["rssi"]
            )

            committed = result[
                "committed"
            ]

            if committed > 0:

                self.total_distance += (
                    committed
                )

                self.events += 1

                if self.last_commit_time is not None:

                    dt = max(
                        0.01,
                        now -
                        self.last_commit_time
                    )

                    self.speed = (
                        committed / dt
                    )

                self.last_commit_time = now

            result["active_ap"] = bssid

            return result

    def state(self):

        with self.lock:

            tracker = (
                self.trackers.get(
                    self.active_bssid
                )
                if self.active_bssid
                else None
            )

            return {
                "distance":
                    self.total_distance,

                "speed":
                    self.speed,

                "events":
                    self.events,

                "active_ap":
                    self.active_bssid,

                "queue_size":
                    (
                        len(
                            tracker.observation_queue
                        )
                        if tracker
                        else 0
                    ),

                "observations_seen":
                    (
                        tracker.observations_seen
                        if tracker
                        else 0
                    ),

                "verified_batches":
                    (
                        tracker.verified_batches
                        if tracker
                        else 0
                    ),

                "rejected_batches":
                    (
                        tracker.rejected_batches
                        if tracker
                        else 0
                    )
            }


# ============================================================
# GUI
# ============================================================

class WifiWalkerApp:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "Wi-Fi Walker v10"
        )

        self.root.geometry(
            "700x520"
        )

        self.root.minsize(
            600,
            450
        )

        self.root.columnconfigure(
            0,
            weight=1
        )

        self.root.rowconfigure(
            4,
            weight=1
        )

        self.interface = (
            find_wifi_interface()
        )

        self.engine = MovementEngine()

        self.running = False

        # Raw scan queue.
        #
        # This may discard an old RAW scan if the processor
        # cannot keep up. It does NOT touch the verification
        # queue inside APTracker.
        self.scan_queue = queue.Queue(
            maxsize=2
        )

        self.gui_queue = queue.Queue()

        self.records = []

        self.build_gui()

        self.root.after(
            30,
            self.process_gui
        )

    def build_gui(self):

        # ----------------------------------------------------
        # Header
        # ----------------------------------------------------

        header = ttk.Frame(
            self.root
        )

        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=12,
            pady=(7, 2)
        )

        header.columnconfigure(
            0,
            weight=1
        )

        ttk.Label(
            header,
            text="Wi-Fi Walker v10",
            font=("TkDefaultFont", 20, "bold")
        ).grid(
            row=0,
            column=0,
            sticky="w"
        )

        ttk.Label(
            header,
            text=(
                "1% = 0.1m • "
                "3-observation FIFO • "
                "noise-tolerant verification"
            )
        ).grid(
            row=1,
            column=0,
            sticky="w"
        )

        # ----------------------------------------------------
        # Config
        # ----------------------------------------------------

        config = ttk.LabelFrame(
            self.root,
            text="Distance Model"
        )

        config.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=12,
            pady=4
        )

        ttk.Label(
            config,
            text="Meters / 1%:"
        ).pack(
            side="left",
            padx=(8, 3),
            pady=3
        )

        self.chunk_entry = ttk.Entry(
            config,
            width=6
        )

        self.chunk_entry.insert(
            0,
            "0.1"
        )

        self.chunk_entry.pack(
            side="left"
        )

        ttk.Label(
            config,
            text="   90% → 50% = 4m"
        ).pack(
            side="left"
        )

        ttk.Label(
            config,
            text=(
                f"Wi-Fi: "
                f"{self.interface or 'NOT FOUND'}"
            )
        ).pack(
            side="right",
            padx=8
        )

        # ----------------------------------------------------
        # Stats
        # ----------------------------------------------------

        stats = ttk.Frame(
            self.root
        )

        stats.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=12,
            pady=3
        )

        for i in range(3):

            stats.columnconfigure(
                i,
                weight=1
            )

        self.distance_label = ttk.Label(
            stats,
            text="0.00 m",
            font=("TkDefaultFont", 22, "bold")
        )

        self.distance_label.grid(
            row=0,
            column=0
        )

        self.speed_label = ttk.Label(
            stats,
            text="0.00 m/s",
            font=("TkDefaultFont", 18, "bold")
        )

        self.speed_label.grid(
            row=0,
            column=1
        )

        self.queue_label = ttk.Label(
            stats,
            text="0",
            font=("TkDefaultFont", 18, "bold")
        )

        self.queue_label.grid(
            row=0,
            column=2
        )

        ttk.Label(
            stats,
            text="DISTANCE"
        ).grid(
            row=1,
            column=0
        )

        ttk.Label(
            stats,
            text="SPEED"
        ).grid(
            row=1,
            column=1
        )

        ttk.Label(
            stats,
            text="QUEUE"
        ).grid(
            row=1,
            column=2
        )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        status = ttk.Frame(
            self.root
        )

        status.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=12
        )

        status.columnconfigure(
            0,
            weight=1
        )

        self.state_label = ttk.Label(
            status,
            text="READY",
            font=("TkDefaultFont", 13, "bold"),
            anchor="center"
        )

        self.state_label.grid(
            row=0,
            column=0,
            sticky="ew"
        )

        self.status_label = ttk.Label(
            status,
            text="Press START",
            anchor="center"
        )

        self.status_label.grid(
            row=1,
            column=0,
            sticky="ew"
        )

        self.batch_label = ttk.Label(
            status,
            text="Batch: —",
            anchor="center"
        )

        self.batch_label.grid(
            row=2,
            column=0,
            sticky="ew"
        )

        # ----------------------------------------------------
        # Network table
        # ----------------------------------------------------

        frame = ttk.LabelFrame(
            self.root,
            text="Wi-Fi Networks"
        )

        frame.grid(
            row=4,
            column=0,
            sticky="nsew",
            padx=12,
            pady=4
        )

        frame.columnconfigure(
            0,
            weight=1
        )

        frame.rowconfigure(
            0,
            weight=1
        )

        columns = (
            "bssid",
            "ssid",
            "rssi",
            "percent",
            "tracked"
        )

        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings"
        )

        headings = {
            "bssid": "BSSID",
            "ssid": "SSID",
            "rssi": "RSSI",
            "percent": "%",
            "tracked": "AP"
        }

        for col in columns:

            self.tree.heading(
                col,
                text=headings[col]
            )

        widths = {
            "bssid": 145,
            "ssid": 220,
            "rssi": 60,
            "percent": 55,
            "tracked": 45
        }

        for col, width in widths.items():

            self.tree.column(
                col,
                width=width,
                minwidth=40,
                stretch=(col == "ssid")
            )

        scrollbar = ttk.Scrollbar(
            frame,
            orient="vertical",
            command=self.tree.yview
        )

        self.tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.tree.grid(
            row=0,
            column=0,
            sticky="nsew"
        )

        scrollbar.grid(
            row=0,
            column=1,
            sticky="ns"
        )

        # ----------------------------------------------------
        # Buttons
        # ----------------------------------------------------

        buttons = ttk.Frame(
            self.root
        )

        buttons.grid(
            row=5,
            column=0,
            sticky="ew",
            padx=12,
            pady=(3, 7)
        )

        buttons.columnconfigure(
            0,
            weight=1
        )

        buttons.columnconfigure(
            1,
            weight=1
        )

        self.start_button = ttk.Button(
            buttons,
            text="START",
            command=self.start
        )

        self.start_button.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, 4)
        )

        self.stop_button = ttk.Button(
            buttons,
            text="STOP / SAVE",
            command=self.stop
        )

        self.stop_button.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(4, 0)
        )

        self.stop_button.config(
            state="disabled"
        )

    # ========================================================
    # START
    # ========================================================

    def start(self):

        global METERS_PER_PERCENT

        if self.running:
            return

        if not self.interface:

            messagebox.showerror(
                "Wi-Fi",
                "No Wi-Fi interface found."
            )

            return

        try:

            value = float(
                self.chunk_entry.get()
            )

            if value <= 0:
                raise ValueError

            METERS_PER_PERCENT = value

        except ValueError:

            messagebox.showerror(
                "Invalid model",
                "Meters per 1% must be positive."
            )

            return

        self.engine = MovementEngine()

        self.records = []

        # Remove stale RAW scans only.
        while True:

            try:
                self.scan_queue.get_nowait()

            except queue.Empty:
                break

        self.running = True

        self.start_button.config(
            state="disabled"
        )

        self.stop_button.config(
            state="normal"
        )

        self.state_label.config(
            text="RUNNING"
        )

        self.status_label.config(
            text="Adaptive scanning..."
        )

        threading.Thread(
            target=self.scanner_worker,
            daemon=True
        ).start()

        threading.Thread(
            target=self.processor_worker,
            daemon=True
        ).start()

    # ========================================================
    # SCANNER
    # ========================================================

    def scanner_worker(self):

        busy_delay = 0.04

        while self.running:

            timestamp = time.time()

            networks, error = scan_wifi(
                self.interface
            )

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            if networks is not None:

                busy_delay = 0.04

                try:

                    self.scan_queue.put_nowait(
                        (
                            timestamp,
                            networks
                        )
                    )

                except queue.Full:

                    # Only raw, not-yet-processed scans can
                    # be discarded here.
                    try:
                        self.scan_queue.get_nowait()
                    except queue.Empty:
                        pass

                    try:
                        self.scan_queue.put_nowait(
                            (
                                timestamp,
                                networks
                            )
                        )
                    except queue.Full:
                        pass

                # Do not wait a fixed 0.1s.
                # Let the driver determine the scan speed.
                time.sleep(
                    SCAN_YIELD
                )

                continue

            # ------------------------------------------------
            # RESOURCE BUSY
            # ------------------------------------------------

            if error == "BUSY":

                self.gui_queue.put(
                    (
                        "scanner_status",
                        "Wi-Fi busy — retrying..."
                    )
                )

                time.sleep(
                    busy_delay
                )

                # Exponential backoff, capped at 500ms.
                busy_delay = min(
                    busy_delay * 1.7,
                    0.50
                )

                continue

            # ------------------------------------------------
            # TIMEOUT
            # ------------------------------------------------

            if error == "TIMEOUT":

                self.gui_queue.put(
                    (
                        "scanner_status",
                        "Wi-Fi scan timeout — retrying..."
                    )
                )

                time.sleep(
                    0.10
                )

                continue

            # ------------------------------------------------
            # OTHER ERROR
            # ------------------------------------------------

            self.gui_queue.put(
                (
                    "error",
                    error
                )
            )

            time.sleep(
                0.20
            )

    # ========================================================
    # PROCESSOR
    # ========================================================

    def processor_worker(self):

        while self.running:

            try:

                timestamp, networks = (
                    self.scan_queue.get(
                        timeout=0.2
                    )
                )

            except queue.Empty:

                continue

            result = self.engine.update(
                networks
            )

            state = self.engine.state()

            self.records.append({
                "timestamp": timestamp,
                "networks": networks,
                "result": result,
                "state": state
            })

            self.gui_queue.put(
                (
                    "update",
                    networks,
                    result,
                    state
                )
            )

    # ========================================================
    # GUI QUEUE
    # ========================================================

    def process_gui(self):

        try:

            while True:

                item = (
                    self.gui_queue.get_nowait()
                )

                if item[0] == "error":

                    self.status_label.config(
                        text=(
                            "SCAN ERROR: "
                            +
                            str(item[1])
                        )
                    )

                elif item[0] == "scanner_status":

                    self.status_label.config(
                        text=item[1]
                    )

                else:

                    (
                        _,
                        networks,
                        result,
                        state
                    ) = item

                    self.update_gui(
                        networks,
                        result,
                        state
                    )

        except queue.Empty:
            pass

        if self.root.winfo_exists():

            self.root.after(
                30,
                self.process_gui
            )

    # ========================================================
    # GUI UPDATE
    # ========================================================

    def update_gui(
        self,
        networks,
        result,
        state
    ):

        self.distance_label.config(
            text=(
                f"{state['distance']:.2f} m"
            )
        )

        self.speed_label.config(
            text=(
                f"{state['speed']:.2f} m/s"
            )
        )

        self.queue_label.config(
            text=str(
                state["queue_size"]
            )
        )

        if result["verified"]:

            self.state_label.config(
                text="✓ VERIFIED + COMMITTED"
            )

            self.status_label.config(
                text=(
                    f"+{result['committed']:.2f}m | "
                    f"net verified movement | "
                    f"queue: {result['queue_size']}"
                )
            )

            values = [
                f"{x['delta_percent']:+.1f}%"
                for x in result["batch"]
            ]

            net = sum(
                x["delta_percent"]
                for x in result["batch"]
            )

            self.batch_label.config(
                text=(
                    "BATCH: "
                    +
                    " → ".join(values)
                    +
                    f" | NET {net:+.1f}%"
                    +
                    f" = {result['committed']:.2f}m"
                )
            )

        elif result["rejected"]:

            self.state_label.config(
                text="✗ NO NET MOVEMENT → QUEUE CONTINUES"
            )

            self.status_label.config(
                text=(
                    "This 3-observation batch looked "
                    "like radio noise; later observations "
                    "remain queued."
                )
            )

        elif result["queue_size"] > 0:

            self.state_label.config(
                text=(
                    f"⏳ VERIFYING "
                    f"{result['queue_size']}/{VERIFY_SIZE}+"
                )
            )

            self.status_label.config(
                text=(
                    f"+{result['candidate']:.2f}m observation "
                    f"added to FIFO"
                )
            )

        else:

            self.state_label.config(
                text="WAITING FOR ≥1% CHANGE"
            )

            self.status_label.config(
                text=(
                    f"AP {result['active_ap'] or '-'}"
                )
            )

        # ----------------------------------------------------
        # TABLE
        # ----------------------------------------------------

        for item in self.tree.get_children():

            self.tree.delete(item)

        active = state["active_ap"]

        for bssid, data in sorted(
            networks.items(),
            key=lambda x: x[1]["rssi"],
            reverse=True
        ):

            self.tree.insert(
                "",
                "end",
                values=(
                    bssid,
                    data.get("ssid", ""),
                    f"{data['rssi']:.1f}",
                    f"{rssi_to_percent(data['rssi']):.1f}",
                    "YES" if bssid == active else ""
                )
            )

    # ========================================================
    # STOP / SAVE
    # ========================================================

    def stop(self):

        self.running = False

        time.sleep(
            0.15
        )

        state = self.engine.state()

        filename = (
            "wifi_walk_v10_"
            +
            time.strftime(
                "%Y%m%d_%H%M%S"
            )
            +
            ".json"
        )

        output = {
            "version": 10,

            "model": {
                "meters_per_percent":
                    METERS_PER_PERCENT,

                "rssi_near_dbm":
                    RSSI_NEAR_DBM,

                "rssi_far_dbm":
                    RSSI_FAR_DBM,

                "verification_size":
                    VERIFY_SIZE,

                "minimum_net_percent":
                    MIN_NET_PERCENT,

                "rssi_alpha":
                    RSSI_ALPHA
            },

            "final_state":
                state,

            "records":
                self.records
        }

        try:

            with open(
                filename,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    output,
                    f,
                    indent=2
                )

        except Exception as e:

            filename = (
                "SAVE ERROR: "
                +
                str(e)
            )

        messagebox.showinfo(
            "Walk saved",
            (
                f"Distance: "
                f"{state['distance']:.2f} m\n\n"
                f"Speed: "
                f"{state['speed']:.2f} m/s\n\n"
                f"Verified batches: "
                f"{state['verified_batches']}\n\n"
                f"Rejected batches: "
                f"{state['rejected_batches']}\n\n"
                f"Observations: "
                f"{state['observations_seen']}\n\n"
                f"Still queued: "
                f"{state['queue_size']}\n\n"
                f"Saved: {filename}"
            )
        )

        self.root.destroy()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = WifiWalkerApp(
        root
    )

    root.mainloop()
