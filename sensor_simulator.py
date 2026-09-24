#!/usr/bin/env python3
"""
sensor_simulator.py - CNC spindle telemetry simulator (MQTT publisher).

Simulates the spindle of a CNC machining centre and publishes one JSON sample
per interval to an MQTT broker:

    factory/shopfloor/<machine_id>/telemetry   JSON sample (QoS 1)
    factory/shopfloor/<machine_id>/status      "ONLINE" / "OFFLINE" (retained, LWT)

The physics are simple but plausible, so the data looks like a real machine:
  * RPM ramps toward a set-point that changes with each "program step".
  * Temperature follows RPM with a first-order thermal lag.
  * Vibration (RMS velocity in mm/s, cf. ISO 10816 / ISO 20816) rises with RPM.

Example:
    python sensor_simulator.py --host localhost --port 1883 --machine-id cnc1
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger("sensor_simulator")


# --------------------------------------------------------------------------- #
# Process model
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    temperature_c: float
    spindle_rpm: int
    vibration_mm_s: float


class SpindleModel:
    """Minimal CNC spindle model: RPM set-points, thermal lag, speed-dependent vibration."""

    SETPOINTS_RPM = (0, 6000, 12000, 18000)  # 0 = spindle stopped (tool change / idle)
    MAX_RPM = 24000.0
    RAMP_RPM_PER_S = 3000.0      # spindle acceleration limit
    AMBIENT_C = 22.0             # shop-floor ambient temperature
    TEMP_RISE_AT_MAX_C = 45.0    # steady-state rise above ambient at MAX_RPM
    THERMAL_TAU_S = 45.0         # thermal time constant of spindle housing
    BASE_VIBRATION_MM_S = 0.3    # structure-borne background vibration
    VIBRATION_AT_MAX_MM_S = 2.4  # additional vibration at MAX_RPM

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._rpm = 0.0
        self._temperature_c = self.AMBIENT_C
        # Start from a cold machine that immediately spins up into its first machining step.
        self._setpoint_rpm = float(rng.choice(self.SETPOINTS_RPM[1:]))
        self._time_to_next_step_s = rng.uniform(15.0, 40.0)

    def step(self, dt: float) -> Sample:
        """Advance the model by ``dt`` seconds and return a noisy measurement."""
        # New machining program step every 15-40 s.
        self._time_to_next_step_s -= dt
        if self._time_to_next_step_s <= 0:
            self._setpoint_rpm = float(self._rng.choice(self.SETPOINTS_RPM))
            self._time_to_next_step_s = self._rng.uniform(15.0, 40.0)
            log.debug("New spindle set-point: %.0f rpm", self._setpoint_rpm)

        # Rate-limited ramp toward the set-point.
        max_delta = self.RAMP_RPM_PER_S * dt
        self._rpm += max(-max_delta, min(max_delta, self._setpoint_rpm - self._rpm))
        load = self._rpm / self.MAX_RPM

        # First-order thermal response (exact discretisation of dT/dt = (T_ss - T) / tau).
        target_c = self.AMBIENT_C + self.TEMP_RISE_AT_MAX_C * load
        alpha = 1.0 - math.exp(-dt / self.THERMAL_TAU_S)
        self._temperature_c += (target_c - self._temperature_c) * alpha

        vibration = self.BASE_VIBRATION_MM_S + self.VIBRATION_AT_MAX_MM_S * load

        # Add sensor noise to the "true" process values.
        return Sample(
            temperature_c=round(self._temperature_c + self._rng.gauss(0.0, 0.05), 2),
            spindle_rpm=max(0, round(self._rpm + self._rng.gauss(0.0, 0.002 * self._rpm))),
            vibration_mm_s=round(max(0.0, vibration + self._rng.gauss(0.0, 0.05)), 3),
        )


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #
def utc_timestamp() -> str:
    """ISO 8601 UTC timestamp with millisecond resolution, e.g. 2026-09-24T13:45:01.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_client(args: argparse.Namespace, status_topic: str, connected: threading.Event) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"sensor-sim-{args.machine_id}",
        protocol=mqtt.MQTTv311,
    )
    # Last Will: the broker publishes OFFLINE for us if we drop off the network uncleanly.
    client.will_set(status_topic, payload="OFFLINE", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker rejected connection: %s", reason_code)
            return
        log.info("Connected to MQTT broker %s:%d", args.host, args.port)
        client.publish(status_topic, payload="ONLINE", qos=1, retain=True)
        connected.set()

    def on_connect_fail(client, userdata):
        log.warning("Broker %s:%d unreachable, retrying...", args.host, args.port)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        connected.clear()
        if reason_code.is_failure:
            log.warning("Lost connection to broker (%s), reconnecting...", reason_code)
        else:
            log.info("Disconnected from broker")

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    return client


def run(args: argparse.Namespace) -> int:
    base_topic = f"factory/shopfloor/{args.machine_id}"
    telemetry_topic = f"{base_topic}/telemetry"
    status_topic = f"{base_topic}/status"

    stop = threading.Event()

    def request_stop(signum, _frame):
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    connected = threading.Event()
    client = build_client(args, status_topic, connected)
    model = SpindleModel(random.Random(args.seed))

    # connect_async + loop_start: the network thread keeps retrying if the broker is down.
    client.connect_async(args.host, args.port, keepalive=30)
    client.loop_start()

    # Don't start the process clock until the first connection is up.
    while not stop.is_set() and not connected.wait(timeout=0.5):
        pass
    log.info("Publishing to '%s' every %.1f s (Ctrl+C to stop)", telemetry_topic, args.interval)

    seq = 0
    dropped = 0
    next_tick = time.monotonic()
    try:
        while not stop.is_set():
            sample = model.step(args.interval)
            seq += 1

            if connected.is_set():
                payload = json.dumps({
                    "machine_id": args.machine_id,
                    "seq": seq,
                    "timestamp": utc_timestamp(),
                    "temperature_c": sample.temperature_c,
                    "spindle_rpm": sample.spindle_rpm,
                    "vibration_mm_s": sample.vibration_mm_s,
                })
                info = client.publish(telemetry_topic, payload, qos=args.qos)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    log.info("seq=%-5d T=%6.2f °C  n=%5d rpm  v=%5.3f mm/s",
                             seq, sample.temperature_c, sample.spindle_rpm, sample.vibration_mm_s)
                else:
                    log.warning("Publish failed for seq=%d: %s", seq, mqtt.error_string(info.rc))
            else:
                # Telemetry is time-series data: drop while offline instead of queueing
                # unboundedly. Gaps stay visible downstream through the seq counter.
                dropped += 1
                log.debug("Not connected, dropped sample seq=%d", seq)

            # Schedule against a monotonic clock so the rate does not drift.
            next_tick += args.interval
            stop.wait(max(0.0, next_tick - time.monotonic()))
    finally:
        if connected.is_set():
            # A clean disconnect does not trigger the Last Will, so announce OFFLINE ourselves.
            info = client.publish(status_topic, payload="OFFLINE", qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=2.0)
            except (RuntimeError, ValueError) as exc:
                log.warning("Could not publish OFFLINE status: %s", exc)
        client.disconnect()
        client.loop_stop()
        log.info("Stopped after %d samples (%d dropped while offline)", seq, dropped)

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CNC spindle MQTT telemetry simulator")
    parser.add_argument("--host", default="localhost", help="MQTT broker host (default: localhost)")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    parser.add_argument("--machine-id", default="cnc1", help="machine identifier used in the topic (default: cnc1)")
    parser.add_argument("--interval", type=float, default=1.0, help="publish interval in seconds (default: 1.0)")
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=1, help="MQTT QoS for telemetry (default: 1)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for reproducible runs")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
