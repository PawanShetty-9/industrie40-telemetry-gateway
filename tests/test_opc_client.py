"""Tests for opc_client.py against a real OPC UA server started in-process.

Run from the repository root:
    python -m unittest -v
"""

import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import edge_gateway as gw
import opc_client
import opc_ua_server

URL = "opc.tcp://127.0.0.1:48410"


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class OpcClientTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._pki = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls._pki.cleanup()

    async def asyncSetUp(self):
        self.server = await opc_ua_server.create_server(URL, pki_dir=Path(self._pki.name))
        nodes = await opc_ua_server.build_information_model(self.server)
        await self.server.start()
        self.gateway = gw.EdgeGateway(nodes, "cnc1", stale_timeout=5.0)

    async def asyncTearDown(self):
        await self.server.stop()

    def args(self, *extra: str):
        return opc_client.parse_args(["--url", URL, "--pki-dir", self._pki.name, "--check", "--timeout", "2", *extra])

    async def publish_sample(self):
        now = datetime.now(timezone.utc)
        payload = json.dumps({
            "machine_id": "cnc1", "seq": 1, "timestamp": now.isoformat(),
            "temperature_c": 40.0, "spindle_rpm": 12000, "vibration_mm_s": 1.5,
        }).encode()
        await self.gateway.process_event(gw.MqttMessage(self.gateway.telemetry_topic, payload, now))

    async def run_client(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = await opc_client.run(self.args(*extra))
        return code, out.getvalue()

    async def test_discovers_signals_with_metadata(self):
        client = await opc_client.connect(self.args(), deadline=None)
        try:
            _, signals = await opc_client.discover_signals(client, opc_client.DEFAULT_MACHINE_PATH)
        finally:
            await client.disconnect()
        self.assertEqual(
            [(s.name, s.data_type, s.unit, s.eu_low, s.eu_high) for s in signals],
            [("Temperature", "Double", "°C", 0.0, 120.0),
             ("RPM", "Double", "r/min", 0.0, 24000.0),
             ("Vibration", "Double", "mm/s", 0.0, 50.0)],
        )

    async def test_check_fails_while_waiting_for_data(self):
        code, output = await self.run_client()
        self.assertEqual(code, opc_client.EXIT_CHECK_FAILED)
        self.assertIn("BadWaitingForInitialData", output)

    async def test_check_passes_once_values_are_good(self):
        await self.publish_sample()
        code, output = await self.run_client()
        self.assertEqual(code, opc_client.EXIT_OK, output)
        self.assertIn("CHECK PASSED", output)

    async def test_check_passes_over_encrypted_connection(self):
        await self.publish_sample()
        code, output = await self.run_client("--security", "Aes256Sha256RsaPss")
        self.assertEqual(code, opc_client.EXIT_OK, output)

    async def test_unknown_machine_path_fails(self):
        code, output = await self.run_client("--machine-path", "Factory/ProductionLine1/CNC_Machine_9")
        self.assertEqual(code, opc_client.EXIT_CHECK_FAILED)
        self.assertIn("not found", output)

    async def test_unreachable_server_returns_connection_error(self):
        out = io.StringIO()
        args = opc_client.parse_args(["--url", "opc.tcp://127.0.0.1:48419", "--check", "--timeout", "1"])
        with contextlib.redirect_stderr(out):
            code = await opc_client.run(args)
        self.assertEqual(code, opc_client.EXIT_CONNECTION)


if __name__ == "__main__":
    unittest.main()
