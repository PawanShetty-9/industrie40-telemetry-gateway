#!/usr/bin/env python3
"""
opc_client.py - OPC UA verification client for the edge gateway.

Behaves like a generic OPC UA client (SCADA, MES): it only knows the
namespace URI and the browse path of the machine. Which signals exist, their
data types, units and ranges are all discovered from the server's
information model at runtime.

    python opc_client.py                              # live view (unencrypted endpoint)
    python opc_client.py --security Basic256Sha256    # Sign & Encrypt with a client certificate
    python opc_client.py --check --timeout 20         # smoke test: exit 0 once all values are Good

Exit codes: 0 = OK, 1 = check failed, 2 = cannot connect / connection lost.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from asyncua import Client, Node, ua
from asyncua.crypto import security_policies

import opc_ua_server

CLIENT_NAME = "Edge Gateway Verification Client"
CLIENT_APP_URI = "urn:industrie40-telemetry-gateway:verification-client"
DEFAULT_MACHINE_PATH = "Factory/ProductionLine1/CNC_Machine_1"
SECURITY_POLICIES = {
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256Sha256RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}
EXIT_OK, EXIT_CHECK_FAILED, EXIT_CONNECTION = 0, 1, 2

HINTS = {
    ua.StatusCodes.BadWaitingForInitialData:
        "no data yet: is sensor_simulator.py running with the same --machine-id?",
    ua.StatusCodes.UncertainLastUsableValue:
        "the data source stopped updating: check the simulator and the MQTT broker",
    ua.StatusCodes.UncertainEngineeringUnitsExceeded:
        "value is outside its EURange",
}


@dataclass(frozen=True)
class Signal:
    name: str
    node: Node
    data_type: str
    unit: str
    eu_low: float
    eu_high: float


class ConnectionFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
async def create_client(args: argparse.Namespace) -> Client:
    client = Client(args.url, timeout=5)
    client.session_timeout = 600_000  # 10 min, the maximum the server grants
    if args.security != "none":
        cert, key = opc_ua_server.ensure_certificate(args.pki_dir / "client", "client", CLIENT_NAME, CLIENT_APP_URI)
        client.application_uri = CLIENT_APP_URI  # must match the URI in the client certificate
        await client.set_security(
            SECURITY_POLICIES[args.security], str(cert), str(key), mode=ua.MessageSecurityMode.SignAndEncrypt
        )
    return client


async def connect(args: argparse.Namespace, deadline: float | None) -> Client:
    """Connect once, or keep retrying until ``deadline`` (time.monotonic())."""
    while True:
        client = await create_client(args)
        try:
            await client.connect()
            return client
        except (OSError, asyncio.TimeoutError, ua.UaError) as exc:
            if deadline is None or time.monotonic() >= deadline:
                raise ConnectionFailed(f"cannot connect to {args.url}: {exc or type(exc).__name__}") from None
            await asyncio.sleep(1.0)


# --------------------------------------------------------------------------- #
# Information model discovery
# --------------------------------------------------------------------------- #
async def _read_property(node: Node, name: str):
    try:
        return await (await node.get_child(f"0:{name}")).read_value()
    except ua.UaStatusCodeError:
        return None  # optional property not present


async def discover_signals(client: Client, machine_path: str) -> tuple[Node, list[Signal]]:
    """Resolve the machine by namespace URI + browse path and list its AnalogItemType variables."""
    idx = await client.get_namespace_index(opc_ua_server.NAMESPACE_URI)
    machine = await client.nodes.objects.get_child([f"{idx}:{part}" for part in machine_path.split("/")])

    signals = []
    analog_item_type = ua.NodeId(ua.ObjectIds.AnalogItemType)
    for child in await machine.get_children(refs=ua.ObjectIds.HasComponent, nodeclassmask=ua.NodeClass.Variable):
        if await child.read_type_definition() != analog_item_type:
            continue
        eu_range = await _read_property(child, "EURange")
        units = await _read_property(child, "EngineeringUnits")
        signals.append(Signal(
            name=(await child.read_browse_name()).Name,
            node=child,
            data_type=(await child.read_data_type_as_variant_type()).name,
            unit=units.DisplayName.Text if units else "",
            eu_low=eu_range.Low if eu_range else float("-inf"),
            eu_high=eu_range.High if eu_range else float("inf"),
        ))
    return machine, signals


async def print_overview(client: Client, args: argparse.Namespace, machine: Node, signals: list[Signal]) -> None:
    state = await client.get_node(ua.ObjectIds.Server_ServerStatus_State).read_value()
    product = await client.get_node(ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductName).read_value()
    version = await client.get_node(ua.ObjectIds.Server_ServerStatus_BuildInfo_SoftwareVersion).read_value()
    type_name = (await client.get_node(await machine.read_type_definition()).read_browse_name()).Name
    security = "None" if args.security == "none" else f"{args.security}, Sign & Encrypt"

    print(f"Connected: {args.url}  (security: {security})")
    print(f"Server:    {product} {version}, state {ua.ServerState(state).name}")
    print(f"Machine:   Objects/{args.machine_path}  [{type_name}]")
    print(f"  {'Signal':<12} {'Type':<7} {'Unit':<6} {'EURange':<14} NodeId")
    for s in signals:
        eu_range = f"{s.eu_low:g} … {s.eu_high:g}"
        print(f"  {s.name:<12} {s.data_type:<7} {s.unit:<6} {eu_range:<14} {s.node.nodeid.to_string()}")
    print(flush=True)


# --------------------------------------------------------------------------- #
# Live values
# --------------------------------------------------------------------------- #
def format_value_line(sig: Signal, dv: ua.DataValue) -> str:
    value = "n/a" if dv.Value is None or dv.Value.Value is None else f"{dv.Value.Value:g}"
    source = dv.SourceTimestamp.strftime("%H:%M:%S.%f")[:-3] if dv.SourceTimestamp else "-"
    line = (f"{datetime.now():%H:%M:%S}  {sig.name:<12} {value:>10} {sig.unit:<6} "
            f"{dv.StatusCode.name:<35} source {source}")
    fresh = dv.StatusCode.is_good() or dv.StatusCode.value == ua.StatusCodes.UncertainEngineeringUnitsExceeded
    if fresh and dv.SourceTimestamp and dv.ServerTimestamp:
        # SourceTimestamp = measured by the sensor, ServerTimestamp = received by the gateway
        delay_ms = (dv.ServerTimestamp - dv.SourceTimestamp).total_seconds() * 1000
        line += f"  transport delay {delay_ms:.1f} ms"
    return line


class ValuePrinter:
    """Subscription handler: prints every data change and tracks the latest value per signal."""

    def __init__(self, signals: list[Signal]) -> None:
        self._by_node = {s.node.nodeid: s for s in signals}
        self.latest: dict[str, ua.DataValue] = {}
        self.all_good = asyncio.Event()

    def datachange_notification(self, node: Node, val, data) -> None:
        sig = self._by_node[node.nodeid]
        dv = data.monitored_item.Value
        self.latest[sig.name] = dv
        print(format_value_line(sig, dv), flush=True)
        if len(self.latest) == len(self._by_node) and all(v.StatusCode.is_good() for v in self.latest.values()):
            self.all_good.set()

    def status_change_notification(self, status: ua.StatusChangeNotification) -> None:
        print(f"Subscription status changed: {status.Status.name}", flush=True)


async def check(printer: ValuePrinter, signals: list[Signal], deadline: float) -> int:
    """Pass once every signal has reported Good; otherwise explain what is wrong."""
    try:
        await asyncio.wait_for(printer.all_good.wait(), timeout=max(0.1, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        print("\nCHECK FAILED: not all signals are Good")
        for s in signals:
            dv = printer.latest.get(s.name)
            if dv is None:
                print(f"  {s.name:<12} {'no notification':<35}")
            else:
                print(f"  {s.name:<12} {dv.StatusCode.name:<35} {HINTS.get(dv.StatusCode.value, '')}")
        return EXIT_CHECK_FAILED
    print(f"\nCHECK PASSED: all {len(signals)} signals are Good")
    return EXIT_OK


async def run(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    deadline = time.monotonic() + args.timeout
    try:
        # In --check mode the gateway may still be starting, so keep retrying until the deadline.
        client = await connect(args, deadline if args.check else None)
    except ConnectionFailed as exc:
        print(f"ERROR: {exc}\nIs edge_gateway.py running? With --secure-only, use --security.", file=sys.stderr)
        return EXIT_CONNECTION

    lost = asyncio.Event()

    async def on_connection_lost(exc: Exception) -> None:
        print(f"\nERROR: connection to {args.url} lost ({exc!r})", file=sys.stderr)
        lost.set()

    client.connection_lost_callback = on_connection_lost
    try:
        try:
            machine, signals = await discover_signals(client, args.machine_path)
        except (ValueError, ua.UaStatusCodeError) as exc:
            print(f"ERROR: machine 'Objects/{args.machine_path}' not found in namespace "
                  f"{opc_ua_server.NAMESPACE_URI} ({exc})", file=sys.stderr)
            return EXIT_CHECK_FAILED
        if not signals:
            print(f"ERROR: no AnalogItemType signals under Objects/{args.machine_path}", file=sys.stderr)
            return EXIT_CHECK_FAILED
        await print_overview(client, args, machine, signals)

        printer = ValuePrinter(signals)
        subscription = await client.create_subscription(250, printer)
        await subscription.subscribe_data_change([s.node for s in signals])

        if args.check:
            return await check(printer, signals, deadline)

        print("Live values (Ctrl+C to stop):", flush=True)
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait())]
        done, pending = await asyncio.wait(waiters, timeout=args.duration, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        return EXIT_CONNECTION if lost.is_set() else EXIT_OK
    finally:
        if not lost.is_set():
            try:
                await client.disconnect()
            except (OSError, asyncio.TimeoutError, ua.UaError):
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OPC UA verification client for the edge gateway")
    parser.add_argument("--url", default=opc_ua_server.DEFAULT_ENDPOINT,
                        help=f"server endpoint URL (default: {opc_ua_server.DEFAULT_ENDPOINT})")
    parser.add_argument("--security", default="none", choices=("none", *SECURITY_POLICIES),
                        help="security policy; anything but 'none' uses Sign & Encrypt (default: none)")
    parser.add_argument("--machine-path", default=DEFAULT_MACHINE_PATH,
                        help=f"browse path of the machine below Objects (default: {DEFAULT_MACHINE_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="smoke test: exit 0 as soon as all signals are Good, 1 on timeout")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="--check: seconds to wait for connection and Good values (default: 15)")
    parser.add_argument("--duration", type=float, default=None,
                        help="live view: stop after this many seconds (default: until Ctrl+C)")
    parser.add_argument("--pki-dir", type=Path, default=opc_ua_server.DEFAULT_PKI_DIR,
                        help="directory for the client certificate (default: ./pki)")
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # asyncua logs expected events (e.g. a closed connection) as errors; keep them quiet.
    logging.getLogger("asyncua").setLevel(logging.DEBUG if args.log_level == "DEBUG" else logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
