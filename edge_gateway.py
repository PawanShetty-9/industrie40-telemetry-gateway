#!/usr/bin/env python3
"""
edge_gateway.py - MQTT -> OPC UA edge gateway.

Subscribes to the CNC telemetry published by sensor_simulator.py and serves it
through the OPC UA information model of opc_ua_server.py. Both sides run in
one process, like a commercial edge gateway: OPC UA clients get read-only
access and only the gateway itself writes values.

    MQTT broker -> paho network thread -> asyncio.Queue -> consumer task -> OPC UA nodes
                   (on_message)           (thread-safe)    (validate, map, write)

Data quality is reported with standard OPC UA status codes:
    Bad_WaitingForInitialData            nothing received yet
    Good                                 fresh, valid sample
    Uncertain_EngineeringUnitsExceeded   value outside the node's EURange
    Uncertain_LastUsableValue            source stopped updating (watchdog timeout,
                                         publisher OFFLINE or MQTT broker lost)

Example:
    python edge_gateway.py --mqtt-host localhost --opcua-endpoint opc.tcp://localhost:4840
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from asyncua import Node, ua
from paho.mqtt.enums import CallbackAPIVersion

import opc_ua_server
from opc_ua_server import CNC_SIGNALS

log = logging.getLogger("edge_gateway")

# JSON field in the MQTT payload -> OPC UA variable of CNCMachineType.
FIELD_TO_NODE = {
    "temperature_c": "Temperature",
    "spindle_rpm": "RPM",
    "vibration_mm_s": "Vibration",
}
MAX_PAYLOAD_BYTES = 4096                  # a telemetry sample is ~200 bytes
MAX_CLOCK_SKEW = timedelta(seconds=60)    # reject samples dated further in the future
MAX_QUEUE_SIZE = 1000                     # back-pressure limit for the MQTT -> OPC UA hand-off
STATS_INTERVAL_S = 30.0

GOOD = ua.StatusCode(ua.StatusCodes.Good)
EU_EXCEEDED = ua.StatusCode(ua.StatusCodes.UncertainEngineeringUnitsExceeded)
LAST_USABLE = ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue)


# --------------------------------------------------------------------------- #
# Payload validation (pure functions, unit tested)
# --------------------------------------------------------------------------- #
class PayloadError(ValueError):
    """An MQTT telemetry payload failed validation."""


@dataclass(frozen=True)
class TelemetrySample:
    seq: int | None
    source_timestamp: datetime
    values: dict[str, float]  # keyed by OPC UA browse name


def _reject_non_finite(token: str) -> None:
    raise PayloadError(f"non-finite number {token} is not allowed")


def parse_timestamp(value: object, received_at: datetime) -> datetime:
    """Parse the ISO 8601 source timestamp; fall back to the receive time if absent."""
    if value is None:
        return received_at
    if not isinstance(value, str):
        raise PayloadError("timestamp must be an ISO 8601 string")
    try:
        # Python < 3.11 does not accept the "Z" suffix.
        ts = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise PayloadError(f"timestamp {value[:40]!r} is not ISO 8601") from None
    if ts.tzinfo is None:
        raise PayloadError("timestamp must include a UTC offset")
    ts = ts.astimezone(timezone.utc)
    # A single sample dated in the future would block all newer ones (see ordering check).
    if ts - received_at > MAX_CLOCK_SKEW:
        raise PayloadError(f"timestamp {value} is in the future (publisher clock skew?)")
    return ts


def parse_telemetry(payload: bytes, expected_machine_id: str, received_at: datetime) -> TelemetrySample:
    """Validate one telemetry payload. MQTT input is untrusted, so everything is checked."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PayloadError(f"payload too large ({len(payload)} bytes)")
    try:
        data = json.loads(payload, parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PayloadError(f"invalid JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise PayloadError("payload must be a JSON object")

    machine_id = data.get("machine_id")
    if machine_id != expected_machine_id:
        raise PayloadError(f"machine_id {machine_id!r} does not match topic machine {expected_machine_id!r}")

    seq = data.get("seq")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        raise PayloadError("seq must be a non-negative integer")

    values: dict[str, float] = {}
    for field_name, node_name in FIELD_TO_NODE.items():
        raw = data.get(field_name)
        if raw is None:
            raise PayloadError(f"missing field {field_name!r}")
        # bool is a subclass of int in Python, so exclude it explicitly.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PayloadError(f"{field_name} must be a number, got {type(raw).__name__}")
        try:
            value = float(raw)
        except OverflowError:
            raise PayloadError(f"{field_name} is out of range") from None
        if not math.isfinite(value):  # e.g. 1e400 parses to inf
            raise PayloadError(f"{field_name} is not a finite number")
        values[node_name] = value

    return TelemetrySample(seq, parse_timestamp(data.get("timestamp"), received_at), values)


# --------------------------------------------------------------------------- #
# Events handed from the MQTT thread (and the watchdog) to the consumer task
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes
    received_at: datetime


@dataclass(frozen=True)
class BrokerLinkLost:
    reason: str


@dataclass(frozen=True)
class WatchdogCheck:
    pass


Event = MqttMessage | BrokerLinkLost | WatchdogCheck


@dataclass
class Stats:
    received: int = 0
    applied: int = 0
    rejected: int = 0
    out_of_order: int = 0
    lost: int = 0       # samples missing according to seq gaps
    overflow: int = 0   # messages dropped because the queue was full


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #
class EdgeGateway:
    """Maps the MQTT telemetry of one machine onto its OPC UA nodes.

    All node writes happen in a single consumer task, so data samples, status
    changes and watchdog timeouts can never interleave.
    """

    def __init__(self, nodes: dict[str, Node], machine_id: str, stale_timeout: float) -> None:
        self._nodes = nodes
        self._machine_id = machine_id
        self._stale_timeout = stale_timeout
        base_topic = f"factory/shopfloor/{machine_id}"
        self.telemetry_topic = f"{base_topic}/telemetry"
        self.status_topic = f"{base_topic}/status"
        self._signals = {sig.name: sig for sig in CNC_SIGNALS}

        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_values: dict[str, ua.DataValue] = {}
        self._last_seq: int | None = None
        self._last_source_ts: datetime | None = None
        self._last_update: float | None = None  # time.monotonic() of the last applied sample
        self._stale = False
        self._eu_exceeded: set[str] = set()
        self.stats = Stats()

    # --- thread hand-off ------------------------------------------------------
    def submit_threadsafe(self, event: Event) -> None:
        """Called from the paho network thread: pass the event to the asyncio loop."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:  # event loop already closed during shutdown
            pass

    def _enqueue(self, event: Event) -> None:
        if isinstance(event, MqttMessage) and self._queue.qsize() >= MAX_QUEUE_SIZE:
            self.stats.overflow += 1
            log.warning("Queue full (%d events), dropping message on %s", MAX_QUEUE_SIZE, event.topic)
            return
        self._queue.put_nowait(event)

    # --- event processing -----------------------------------------------------
    async def process_event(self, event: Event) -> None:
        if isinstance(event, MqttMessage):
            if event.topic == self.status_topic:
                await self._handle_status(event.payload)
            elif event.topic == self.telemetry_topic:
                await self._handle_telemetry(event)
            else:
                log.warning("Ignoring message on unexpected topic %s", event.topic)
        elif isinstance(event, BrokerLinkLost):
            await self._mark_stale(event.reason)
        elif isinstance(event, WatchdogCheck):
            if self._timed_out():
                await self._mark_stale(f"No telemetry for more than {self._stale_timeout:g} s")

    async def _handle_status(self, payload: bytes) -> None:
        status = payload[:32].decode("utf-8", errors="replace").strip()
        if status == "OFFLINE":
            log.warning("Publisher %s reported OFFLINE", self._machine_id)
            await self._mark_stale("Publisher offline")
        elif status == "ONLINE":
            log.info("Publisher %s is ONLINE", self._machine_id)
        else:
            log.warning("Ignoring unknown status %r on %s", status, self.status_topic)

    async def _handle_telemetry(self, msg: MqttMessage) -> None:
        self.stats.received += 1
        try:
            sample = parse_telemetry(msg.payload, self._machine_id, msg.received_at)
        except PayloadError as exc:
            self.stats.rejected += 1
            log.warning("Rejected message on %s: %s", msg.topic, exc)
            return

        # QoS 1 may redeliver messages: an older sample must never overwrite a newer one.
        if self._last_source_ts is not None and sample.source_timestamp <= self._last_source_ts:
            self.stats.out_of_order += 1
            log.debug("Ignoring duplicate/out-of-order sample seq=%s", sample.seq)
            return

        self._track_sequence(sample.seq)
        await self._write_sample(sample)

    def _track_sequence(self, seq: int | None) -> None:
        if seq is None:
            return
        if self._last_seq is not None:
            if seq > self._last_seq + 1:
                missing = seq - self._last_seq - 1
                self.stats.lost += missing
                log.warning("Sequence gap: %d sample(s) lost between seq=%d and seq=%d",
                            missing, self._last_seq, seq)
            elif seq <= self._last_seq:
                log.info("Sequence restarted at seq=%d (publisher restart)", seq)
        self._last_seq = seq

    async def _write_sample(self, sample: TelemetrySample) -> None:
        now = datetime.now(timezone.utc)
        for name, value in sample.values.items():
            sig = self._signals[name]
            in_range = sig.eu_low <= value <= sig.eu_high
            self._log_range_change(name, value, in_range)
            dv = ua.DataValue(
                ua.Variant(value, ua.VariantType.Double),
                StatusCode=GOOD if in_range else EU_EXCEEDED,
                SourceTimestamp=sample.source_timestamp,  # when the sensor measured it
                ServerTimestamp=now,                      # when the gateway received it
            )
            await self._nodes[name].write_value(dv)
            self._last_values[name] = dv

        self._last_source_ts = sample.source_timestamp
        self._last_update = time.monotonic()
        self.stats.applied += 1
        if self._stale:
            self._stale = False
            log.info("Telemetry from %s is flowing again, values are live", self._machine_id)
        log.debug("Applied seq=%s %s", sample.seq, sample.values)

    def _log_range_change(self, name: str, value: float, in_range: bool) -> None:
        sig = self._signals[name]
        if not in_range and name not in self._eu_exceeded:
            self._eu_exceeded.add(name)
            log.warning("%s=%g %s outside EURange [%g, %g]: status Uncertain_EngineeringUnitsExceeded",
                        name, value, sig.unit_symbol, sig.eu_low, sig.eu_high)
        elif in_range and name in self._eu_exceeded:
            self._eu_exceeded.discard(name)
            log.info("%s back inside EURange", name)

    async def _mark_stale(self, reason: str) -> None:
        """Keep the last values but flag them as no longer being updated."""
        if self._stale or not self._last_values:
            return  # already flagged, or still Bad_WaitingForInitialData
        now = datetime.now(timezone.utc)
        for name, last in self._last_values.items():
            await self._nodes[name].write_value(ua.DataValue(
                last.Value,
                StatusCode=LAST_USABLE,
                SourceTimestamp=last.SourceTimestamp,
                ServerTimestamp=now,
            ))
        self._stale = True
        log.warning("%s: OPC UA values marked Uncertain_LastUsableValue", reason)

    def _timed_out(self) -> bool:
        return (not self._stale and self._last_update is not None
                and time.monotonic() - self._last_update > self._stale_timeout)

    def log_stats(self) -> None:
        s = self.stats
        values = "  ".join(
            f"{name}={dv.Value.Value:g} {self._signals[name].unit_symbol} "
            f"[{(LAST_USABLE if self._stale else dv.StatusCode).name}]"
            for name, dv in self._last_values.items()
        ) or "no data yet"
        log.info("Stats: received=%d applied=%d rejected=%d out_of_order=%d lost=%d overflow=%d | %s",
                 s.received, s.applied, s.rejected, s.out_of_order, s.lost, s.overflow, values)

    # --- tasks ------------------------------------------------------------------
    async def _consume(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.process_event(event)
            except Exception:  # one faulty event must never stop the gateway
                log.exception("Unexpected error while processing %s", type(event).__name__)

    async def _monitor(self) -> None:
        next_stats = time.monotonic() + STATS_INTERVAL_S
        while True:
            await asyncio.sleep(0.5)
            if self._timed_out():
                self._enqueue(WatchdogCheck())
            if time.monotonic() >= next_stats:
                self.log_stats()
                next_stats += STATS_INTERVAL_S

    # --- MQTT -------------------------------------------------------------------
    def build_mqtt_client(self, host: str, port: int) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"edge-gateway-{self._machine_id}",
            clean_session=True,  # only current values matter; no backlog after a restart
            protocol=mqtt.MQTTv311,
        )
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log.error("MQTT broker rejected connection: %s", reason_code)
                return
            # Subscribe on every (re)connect: a clean session does not keep subscriptions.
            result, _ = client.subscribe([(self.telemetry_topic, 1), (self.status_topic, 1)])
            if result != mqtt.MQTT_ERR_SUCCESS:
                log.error("MQTT subscribe failed: %s", mqtt.error_string(result))
                return
            log.info("Connected to MQTT broker %s:%d, subscribed to %s and %s",
                     host, port, self.telemetry_topic, self.status_topic)

        def on_connect_fail(client, userdata):
            log.warning("MQTT broker %s:%d unreachable, retrying...", host, port)

        def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
            if reason_code.is_failure:  # not our own disconnect() at shutdown
                log.warning("Lost connection to MQTT broker (%s), reconnecting...", reason_code)
                self.submit_threadsafe(BrokerLinkLost("Lost connection to MQTT broker"))

        def on_message(client, userdata, msg):
            self.submit_threadsafe(MqttMessage(msg.topic, msg.payload, datetime.now(timezone.utc)))

        client.on_connect = on_connect
        client.on_connect_fail = on_connect_fail
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    async def run(self, mqtt_host: str, mqtt_port: int, stop: asyncio.Event) -> int:
        """Run until ``stop`` is set. Returns a process exit code."""
        self._loop = asyncio.get_running_loop()
        client = self.build_mqtt_client(mqtt_host, mqtt_port)
        # connect_async + loop_start: the paho thread keeps retrying if the broker is down.
        client.connect_async(mqtt_host, mqtt_port, keepalive=30)
        client.loop_start()

        workers = [
            asyncio.create_task(self._consume(), name="consumer"),
            asyncio.create_task(self._monitor(), name="monitor"),
        ]
        stop_waiter = asyncio.create_task(stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait([stop_waiter, *workers], return_when=asyncio.FIRST_COMPLETED)
            if stop_waiter in done:
                return 0
            # A worker task should never end. Fail loudly so a supervisor (systemd) restarts us.
            for task in done:
                log.critical("Task %r terminated unexpectedly: %r", task.get_name(), task.exception())
            return 1
        finally:
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
            for task in (stop_waiter, *workers):
                task.cancel()
            await asyncio.gather(stop_waiter, *workers, return_exceptions=True)
            self.log_stats()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    server = await opc_ua_server.create_server(args.opcua_endpoint, args.secure_only, args.pki_dir)
    nodes = await opc_ua_server.build_information_model(server)
    gateway = EdgeGateway(nodes, args.machine_id, args.stale_timeout)

    stop = asyncio.Event()

    def request_stop(signum: int) -> None:
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop, signum)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    try:
        await server.start()
    except OSError as exc:
        log.error("Cannot start OPC UA server on %s: %s (is opc_ua_server.py already running?)",
                  args.opcua_endpoint, exc)
        return 1

    try:
        await opc_ua_server.log_server_info(server, args.opcua_endpoint, nodes)
        return await gateway.run(args.mqtt_host, args.mqtt_port, stop)
    finally:
        await server.stop()
        log.info("Gateway stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MQTT -> OPC UA edge gateway for the CNC simulation")
    parser.add_argument("--mqtt-host", default="localhost", help="MQTT broker host (default: localhost)")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    parser.add_argument("--machine-id", default="cnc1",
                        help="machine id in the MQTT topic, mapped to CNC_Machine_1 (default: cnc1)")
    parser.add_argument("--opcua-endpoint", default=opc_ua_server.DEFAULT_ENDPOINT,
                        help=f"OPC UA endpoint URL (default: {opc_ua_server.DEFAULT_ENDPOINT})")
    parser.add_argument("--secure-only", action="store_true",
                        help="disable the unencrypted (SecurityPolicy None) OPC UA endpoint")
    parser.add_argument("--pki-dir", type=Path, default=opc_ua_server.DEFAULT_PKI_DIR,
                        help="directory for the server certificate and key (default: ./pki)")
    parser.add_argument("--stale-timeout", type=float, default=5.0,
                        help="seconds without telemetry before values become Uncertain (default: 5)")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    if args.stale_timeout <= 0:
        parser.error("--stale-timeout must be > 0")
    if not 1 <= args.mqtt_port <= 65535:
        parser.error("--mqtt-port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # asyncua is very verbose at INFO level; keep its logs to warnings unless debugging.
    logging.getLogger("asyncua").setLevel(logging.DEBUG if args.log_level == "DEBUG" else logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
