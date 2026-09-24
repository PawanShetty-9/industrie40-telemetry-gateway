"""Tests for edge_gateway.py: payload validation and MQTT -> OPC UA mapping.

Run from the repository root:
    python -m unittest -v
"""

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from asyncua import ua

import edge_gateway as gw
import opc_ua_server

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
REMOVE = object()


def make_payload(ts: datetime = T0, **overrides) -> bytes:
    data = {
        "machine_id": "cnc1",
        "seq": 1,
        "timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "temperature_c": 40.0,
        "spindle_rpm": 12000,
        "vibration_mm_s": 1.5,
    }
    data.update(overrides)
    return json.dumps({k: v for k, v in data.items() if v is not REMOVE}).encode()


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class ParseTelemetryTest(unittest.TestCase):
    def test_valid_payload(self):
        sample = gw.parse_telemetry(make_payload(), "cnc1", T0)
        self.assertEqual(sample.seq, 1)
        self.assertEqual(sample.source_timestamp, T0)
        self.assertEqual(sample.values, {"Temperature": 40.0, "RPM": 12000.0, "Vibration": 1.5})

    def test_missing_timestamp_falls_back_to_receive_time(self):
        received = T0 + timedelta(seconds=3)
        sample = gw.parse_telemetry(make_payload(timestamp=REMOVE), "cnc1", received)
        self.assertEqual(sample.source_timestamp, received)

    def test_timestamp_with_offset_is_normalised_to_utc(self):
        sample = gw.parse_telemetry(make_payload(timestamp="2026-09-24T14:00:00+02:00"), "cnc1", T0)
        self.assertEqual(sample.source_timestamp, T0)

    def test_invalid_payloads_are_rejected(self):
        valid = make_payload().decode()
        cases = {
            "invalid JSON": b"{not json",
            "not an object": b"[1, 2, 3]",
            "invalid UTF-8": b"\x80\x81\x82",
            "deeply nested": b"[" * 4000,
            "oversized": b" " * (gw.MAX_PAYLOAD_BYTES + 1),
            "NaN literal": valid.replace("40.0", "NaN").encode(),
            "overflows to inf": valid.replace("40.0", "1e400").encode(),
            "huge integer": valid.replace("12000", "9" * 400).encode(),
            "bool value": make_payload(spindle_rpm=True),
            "string value": make_payload(temperature_c="40.0"),
            "null value": make_payload(vibration_mm_s=None),
            "missing field": make_payload(vibration_mm_s=REMOVE),
            "wrong machine": make_payload(machine_id="cnc2"),
            "negative seq": make_payload(seq=-1),
            "float seq": make_payload(seq=1.5),
            "naive timestamp": make_payload(timestamp="2026-09-24T12:00:00"),
            "garbage timestamp": make_payload(timestamp="yesterday"),
            "numeric timestamp": make_payload(timestamp=1790000000),
            "future timestamp": make_payload(ts=T0 + timedelta(hours=1)),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(gw.PayloadError):
                gw.parse_telemetry(raw, "cnc1", T0)


class GatewayOpcUaTest(unittest.IsolatedAsyncioTestCase):
    """Drives the gateway with MQTT events and checks the resulting OPC UA node values."""

    @classmethod
    def setUpClass(cls):
        cls._pki = tempfile.TemporaryDirectory()  # certificate is generated once and reused

    @classmethod
    def tearDownClass(cls):
        cls._pki.cleanup()

    async def asyncSetUp(self):
        server = await opc_ua_server.create_server("opc.tcp://127.0.0.1:48400", pki_dir=Path(self._pki.name))
        self.nodes = await opc_ua_server.build_information_model(server)
        self.gateway = gw.EdgeGateway(self.nodes, "cnc1", stale_timeout=0.2)

    async def send(self, payload: bytes, topic: str | None = None, received_at: datetime | None = None):
        topic = topic or self.gateway.telemetry_topic
        await self.gateway.process_event(gw.MqttMessage(topic, payload, received_at or T0 + timedelta(seconds=10)))

    async def read(self, name: str) -> ua.DataValue:
        return await self.nodes[name].read_data_value(raise_on_bad_status=False)

    async def test_initial_state_is_waiting_for_data(self):
        for name in gw.FIELD_TO_NODE.values():
            self.assertEqual((await self.read(name)).StatusCode.value, ua.StatusCodes.BadWaitingForInitialData)

    async def test_sample_is_written_with_source_timestamp(self):
        await self.send(make_payload())
        dv = await self.read("RPM")
        self.assertEqual(dv.StatusCode, gw.GOOD)
        self.assertEqual(dv.Value.Value, 12000.0)
        self.assertEqual(dv.Value.VariantType, ua.VariantType.Double)
        self.assertEqual(dv.SourceTimestamp, T0)
        self.assertEqual(self.gateway.stats.applied, 1)

    async def test_value_outside_eurange_is_uncertain(self):
        await self.send(make_payload(temperature_c=150.0))  # EURange is 0..120 °C
        self.assertEqual((await self.read("Temperature")).StatusCode, gw.EU_EXCEEDED)
        self.assertEqual((await self.read("RPM")).StatusCode, gw.GOOD)

    async def test_invalid_payload_leaves_nodes_untouched(self):
        await self.send(make_payload())
        await self.send(b"garbage")
        self.assertEqual((await self.read("Temperature")).Value.Value, 40.0)
        self.assertEqual(self.gateway.stats.rejected, 1)

    async def test_older_sample_never_overwrites_newer(self):
        await self.send(make_payload(ts=T0 + timedelta(seconds=1), seq=2, temperature_c=41.0))
        await self.send(make_payload(ts=T0, seq=1, temperature_c=99.0))  # redelivered / late
        self.assertEqual((await self.read("Temperature")).Value.Value, 41.0)
        self.assertEqual(self.gateway.stats.out_of_order, 1)

    async def test_sequence_gap_is_counted(self):
        await self.send(make_payload(ts=T0, seq=1))
        await self.send(make_payload(ts=T0 + timedelta(seconds=4), seq=5))
        self.assertEqual(self.gateway.stats.lost, 3)

    async def test_publisher_offline_marks_last_values_uncertain(self):
        await self.send(make_payload())
        await self.send(b"OFFLINE", topic=self.gateway.status_topic)
        dv = await self.read("Vibration")
        self.assertEqual(dv.StatusCode, gw.LAST_USABLE)
        self.assertEqual(dv.Value.Value, 1.5)       # last value is kept
        self.assertEqual(dv.SourceTimestamp, T0)    # with its original timestamp

        await self.send(make_payload(ts=T0 + timedelta(seconds=1), seq=2))
        self.assertEqual((await self.read("Vibration")).StatusCode, gw.GOOD)

    async def test_offline_before_first_sample_keeps_waiting_status(self):
        await self.send(b"OFFLINE", topic=self.gateway.status_topic)
        status = (await self.read("RPM")).StatusCode.value
        self.assertEqual(status, ua.StatusCodes.BadWaitingForInitialData)

    async def test_broker_loss_marks_values_uncertain(self):
        await self.send(make_payload())
        await self.gateway.process_event(gw.BrokerLinkLost("Lost connection to MQTT broker"))
        self.assertEqual((await self.read("Temperature")).StatusCode, gw.LAST_USABLE)

    async def test_watchdog_marks_values_uncertain_after_timeout(self):
        await self.send(make_payload())
        await self.gateway.process_event(gw.WatchdogCheck())
        self.assertEqual((await self.read("RPM")).StatusCode, gw.GOOD)  # not timed out yet

        await asyncio.sleep(0.3)  # stale_timeout is 0.2 s
        await self.gateway.process_event(gw.WatchdogCheck())
        self.assertEqual((await self.read("RPM")).StatusCode, gw.LAST_USABLE)


if __name__ == "__main__":
    unittest.main()
