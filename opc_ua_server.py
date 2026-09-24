#!/usr/bin/env python3
"""
opc_ua_server.py - OPC UA server exposing the CNC machine information model.

Address space (namespace "urn:industrie40-telemetry-gateway:factory"):

    Objects
    └── Factory                          FolderType
        └── ProductionLine1              FolderType
            └── CNC_Machine_1            CNCMachineType
                ├── Temperature          AnalogItemType, Double, °C
                ├── RPM                  AnalogItemType, Double, r/min
                └── Vibration            AnalogItemType, Double, mm/s

CNCMachineType is a custom ObjectType, so further machines are just new
instances. Every analog variable carries the standard EURange and
EngineeringUnits properties (UNECE Rec. 20 codes), so any OPC UA client can
label and scale it. Values report Bad_WaitingForInitialData until the edge
gateway writes the first sample.

The module is importable: edge_gateway.py reuses create_server() and
build_information_model() to host the server in its own process.

Run standalone:
    python opc_ua_server.py                  # opc.tcp://localhost:4840
    python opc_ua_server.py --secure-only    # disable the unencrypted endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import signal
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Node, Server, ua
from asyncua.crypto import cert_gen
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

__version__ = "0.3.0"

log = logging.getLogger("opc_ua_server")

DEFAULT_ENDPOINT = "opc.tcp://localhost:4840"
DEFAULT_PKI_DIR = Path(__file__).resolve().parent / "pki"

SERVER_NAME = "Factory Edge Gateway (Simulation)"
APPLICATION_URI = "urn:industrie40-telemetry-gateway:edge-server"
PRODUCT_URI = "https://github.com/PawanShetty-9/industrie40-telemetry-gateway"
NAMESPACE_URI = "urn:industrie40-telemetry-gateway:factory"
UNECE_NAMESPACE_URI = "http://www.opcfoundation.org/UA/units/un/cefact"

# Encrypted endpoints offered to clients. The deprecated Basic128Rsa15 and
# Basic256 policies are intentionally not offered.
SECURE_POLICIES = [
    ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
    ua.SecurityPolicyType.Aes128Sha256RsaOaep_SignAndEncrypt,
    ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt,
]


@dataclass(frozen=True)
class AnalogSignal:
    """Static description of one analog process value (OPC UA AnalogItemType)."""

    name: str
    description: str
    unit_id: int       # UNECE Rec. 20 common code, numeric encoding per OPC 10000-8
    unit_symbol: str
    unit_name: str
    eu_low: float
    eu_high: float


# Unit IDs taken from the OPC Foundation table UNECE_to_OPCUA.csv.
CNC_SIGNALS = (
    AnalogSignal("Temperature", "Spindle housing temperature",
                 4408652, "°C", "degree Celsius", 0.0, 120.0),            # CEL
    AnalogSignal("RPM", "Actual spindle speed",
                 5059638, "r/min", "revolution per minute", 0.0, 24000.0),  # M46
    AnalogSignal("Vibration", "Spindle vibration velocity, RMS (ISO 20816)",
                 4403510, "mm/s", "millimetre per second", 0.0, 50.0),     # C16
)


# --------------------------------------------------------------------------- #
# Security: application instance certificate
# --------------------------------------------------------------------------- #
def ensure_server_certificate(pki_dir: Path) -> tuple[Path, Path]:
    """Return (certificate, private key), creating a self-signed pair on first start.

    The certificate follows OPC 10000-6 §6.2.2: the ApplicationUri is in the
    SubjectAltName, next to the DNS names / IP clients use to reach the server.
    """
    own_dir = pki_dir / "own"
    cert_path = own_dir / "server_cert.der"
    key_path = own_dir / "server_key.pem"

    if cert_path.is_file() and key_path.is_file():
        cert = x509.load_der_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc > datetime.now(timezone.utc):
            return cert_path, key_path
        log.warning("Server certificate expired on %s, generating a new one", cert.not_valid_after_utc)

    own_dir.mkdir(parents=True, exist_ok=True)
    key = cert_gen.generate_private_key()
    cert = cert_gen.generate_self_signed_app_certificate(
        key,
        SERVER_NAME,
        {"organizationName": "industrie40-telemetry-gateway", "countryName": "DE"},
        [
            x509.UniformResourceIdentifier(APPLICATION_URI),
            x509.DNSName(socket.gethostname()),
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ],
        extended=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
        days=365,
    )
    # The private key must be readable by the service account only.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(cert_gen.dump_private_key_as_pem(key))
    cert_path.write_bytes(cert.public_bytes(Encoding.DER))
    log.info("Generated self-signed server certificate: %s", cert_path)
    return cert_path, key_path


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
async def create_server(
    endpoint: str = DEFAULT_ENDPOINT,
    secure_only: bool = False,
    pki_dir: Path = DEFAULT_PKI_DIR,
) -> Server:
    """Create and configure (but do not start) the OPC UA server."""
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name(SERVER_NAME)
    await server.set_application_uri(APPLICATION_URI)
    await server.set_build_info(
        product_uri=PRODUCT_URI,
        manufacturer_name="industrie40-telemetry-gateway",
        product_name=SERVER_NAME,
        software_version=__version__,
        build_number=__version__,
        build_date=datetime.now(timezone.utc),
    )

    policies = list(SECURE_POLICIES)
    if not secure_only:
        policies.insert(0, ua.SecurityPolicyType.NoSecurity)
        log.warning("Unencrypted endpoint (SecurityPolicy None) is enabled for lab use; "
                    "start with --secure-only to disable it")
    server.set_security_policy(policies)
    # The data is read-only, so only anonymous sessions are needed. Offering
    # username tokens would allow passwords to be sent over the None endpoint.
    server.set_identity_tokens([ua.AnonymousIdentityToken])

    cert_path, key_path = ensure_server_certificate(pki_dir)
    await server.load_certificate(str(cert_path))
    await server.load_private_key(str(key_path))
    return server


# --------------------------------------------------------------------------- #
# Information model
# --------------------------------------------------------------------------- #
async def add_analog_item(parent: Node, idx: int, sig: AnalogSignal) -> list[Node]:
    """Add a read-only Double AnalogItemType variable with EURange and EngineeringUnits.

    Returns the variable followed by its two properties.
    """
    nodeid = ua.NodeId(f"{parent.nodeid.Identifier}.{sig.name}", idx)
    item = ua.AddNodesItem(
        ParentNodeId=parent.nodeid,
        ReferenceTypeId=ua.NodeId(ua.ObjectIds.HasComponent),
        RequestedNewNodeId=nodeid,
        BrowseName=ua.QualifiedName(sig.name, idx),
        NodeClass=ua.NodeClass.Variable,
        NodeAttributes=ua.VariableAttributes(
            DisplayName=ua.LocalizedText(sig.name),
            Description=ua.LocalizedText(sig.description),
            Value=ua.Variant(0.0, ua.VariantType.Double),
            DataType=ua.NodeId(ua.ObjectIds.Double),
            ValueRank=ua.ValueRank.Scalar,
            AccessLevel=ua.AccessLevel.CurrentRead.mask,
            UserAccessLevel=ua.AccessLevel.CurrentRead.mask,
        ),
        TypeDefinition=ua.NodeId(ua.ObjectIds.AnalogItemType),
    )
    result = (await parent.session.add_nodes([item]))[0]
    result.StatusCode.check()
    var = Node(parent.session, result.AddedNodeId)

    eu_range = await var.add_property(
        ua.NodeId(f"{nodeid.Identifier}.EURange", idx),
        ua.QualifiedName("EURange", 0),
        ua.Range(Low=sig.eu_low, High=sig.eu_high),
        datatype=ua.ObjectIds.Range,
    )
    eng_units = await var.add_property(
        ua.NodeId(f"{nodeid.Identifier}.EngineeringUnits", idx),
        ua.QualifiedName("EngineeringUnits", 0),
        ua.EUInformation(
            NamespaceUri=UNECE_NAMESPACE_URI,
            UnitId=sig.unit_id,
            DisplayName=ua.LocalizedText(sig.unit_symbol),
            Description=ua.LocalizedText(sig.unit_name),
        ),
        datatype=ua.ObjectIds.EUInformation,
    )
    return [var, eu_range, eng_units]


async def add_cnc_machine_type(server: Server, idx: int) -> Node:
    """Define CNCMachineType: every instance gets the analog signals in CNC_SIGNALS."""
    machine_type = await server.nodes.base_object_type.add_object_type(
        ua.NodeId("CNCMachineType", idx), ua.QualifiedName("CNCMachineType", idx)
    )
    for sig in CNC_SIGNALS:
        # Mandatory modelling rule: instantiation copies these nodes into every machine.
        for node in await add_analog_item(machine_type, idx, sig):
            await node.set_modelling_rule(True)
    return machine_type


async def add_cnc_machine(parent: Node, machine_type: Node, idx: int, name: str) -> dict[str, Node]:
    """Instantiate a CNC machine under ``parent`` and return its signal nodes by name."""
    parent_id = parent.nodeid.Identifier
    machine = await parent.add_object(
        ua.NodeId(f"{parent_id}.{name}", idx), ua.QualifiedName(name, idx), objecttype=machine_type.nodeid
    )
    nodes = {}
    for sig in CNC_SIGNALS:
        node = await machine.get_child(ua.QualifiedName(sig.name, idx))
        # No data yet: say so explicitly instead of publishing a fake 0.0.
        await node.write_value(ua.DataValue(
            ua.Variant(0.0, ua.VariantType.Double),
            StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData),
        ))
        nodes[sig.name] = node
    return nodes


async def build_information_model(server: Server) -> dict[str, Node]:
    """Build Objects/Factory/ProductionLine1/CNC_Machine_1 and return its signal nodes."""
    idx = await server.register_namespace(NAMESPACE_URI)
    machine_type = await add_cnc_machine_type(server, idx)

    factory = await server.nodes.objects.add_folder(
        ua.NodeId("Factory", idx), ua.QualifiedName("Factory", idx)
    )
    line = await factory.add_folder(
        ua.NodeId("Factory.ProductionLine1", idx), ua.QualifiedName("ProductionLine1", idx)
    )
    return await add_cnc_machine(line, machine_type, idx, "CNC_Machine_1")


# --------------------------------------------------------------------------- #
# Standalone entry point
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    server = await create_server(args.endpoint, args.secure_only, args.pki_dir)
    nodes = await build_information_model(server)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    try:
        await server.start()
    except OSError as exc:
        log.error("Cannot start OPC UA server on %s: %s (is the port already in use?)", args.endpoint, exc)
        return 1

    try:
        log.info("OPC UA server listening on %s", args.endpoint)
        for endpoint in await server.get_endpoints():
            log.info("  endpoint: %s", endpoint.SecurityPolicyUri.rsplit("#", 1)[-1]
                     + f" / {endpoint.SecurityMode.name}")
        for name, node in nodes.items():
            log.info("  node:     %-12s %s", name, node.nodeid.to_string())
        log.info("Press Ctrl+C to stop")
        await stop.wait()
    finally:
        await server.stop()
        log.info("Server stopped")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OPC UA server for the factory edge gateway")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help=f"endpoint URL to listen on (default: {DEFAULT_ENDPOINT})")
    parser.add_argument("--secure-only", action="store_true",
                        help="disable the unencrypted (SecurityPolicy None) endpoint")
    parser.add_argument("--pki-dir", type=Path, default=DEFAULT_PKI_DIR,
                        help="directory for the server certificate and key (default: ./pki)")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args(argv)


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
