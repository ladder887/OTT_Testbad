"""Generate the canonical 10-host, 100-logical-client deployment inventory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SUBNET = ipaddress.ip_network("192.168.0.0/24")
GATEWAY = ipaddress.ip_address("192.168.0.1")
PHYSICAL_HOST_START = 131
LOGICAL_CLIENT_START = 151
PHYSICAL_HOST_COUNT = 10
CLIENTS_PER_HOST = 10

EDGES = (
    ("edge-kr", "http://192.168.0.111"),
    ("edge-jp", "http://192.168.0.112"),
    ("edge-sg", "http://192.168.0.113"),
    ("edge-us", "http://192.168.0.114"),
)
NETWORK_PROFILES = ("P0", "P1", "P2", "P3", "P4")
RESERVED_IPS = {
    "192.168.0.1",
    "192.168.0.101",
    "192.168.0.111",
    "192.168.0.112",
    "192.168.0.113",
    "192.168.0.114",
    "192.168.0.120",
    "192.168.0.130",
    *(f"192.168.0.{last}" for last in range(131, 141)),
    "192.168.0.150",
}


def opaque_device_id(index: int) -> str:
    digest = hashlib.sha256(f"ott-device-{index:03d}".encode("ascii")).hexdigest()
    return f"device_{digest[:16]}"


@dataclass(frozen=True)
class LogicalClient:
    logical_client_index: int
    logical_client_id: str
    physical_host_id: str
    physical_host_ip: str
    host_slot: int
    source_ip: str
    account_key: str
    account_email: str
    device_id: str
    edge_id: str
    edge_base_url: str
    network_profile_id: str


def build_inventory() -> list[LogicalClient]:
    clients: list[LogicalClient] = []
    total_clients = PHYSICAL_HOST_COUNT * CLIENTS_PER_HOST
    for index in range(1, total_clients + 1):
        host_number = ((index - 1) // CLIENTS_PER_HOST) + 1
        host_slot = ((index - 1) % CLIENTS_PER_HOST) + 1
        edge_id, edge_base_url = EDGES[(index - 1) % len(EDGES)]
        clients.append(
            LogicalClient(
                logical_client_index=index,
                logical_client_id=f"lc{index:03d}",
                physical_host_id=f"pi{host_number:02d}",
                physical_host_ip=f"192.168.0.{PHYSICAL_HOST_START + host_number - 1}",
                host_slot=host_slot,
                source_ip=f"192.168.0.{LOGICAL_CLIENT_START + index - 1}",
                account_key=f"user{index}",
                account_email=f"user{index}@test.com",
                device_id=opaque_device_id(index),
                edge_id=edge_id,
                edge_base_url=edge_base_url,
                network_profile_id=NETWORK_PROFILES[(index - 1) % len(NETWORK_PROFILES)],
            )
        )
    validate_clients(clients)
    return clients


def validate_clients(clients: list[LogicalClient]) -> None:
    expected_count = PHYSICAL_HOST_COUNT * CLIENTS_PER_HOST
    if len(clients) != expected_count:
        raise ValueError(f"expected {expected_count} clients, got {len(clients)}")

    unique_fields = (
        "logical_client_id",
        "source_ip",
        "account_key",
        "account_email",
        "device_id",
    )
    for field in unique_fields:
        values = [getattr(client, field) for client in clients]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate values in {field}")

    for client in clients:
        source_ip = ipaddress.ip_address(client.source_ip)
        if source_ip not in SUBNET:
            raise ValueError(f"{client.source_ip} is outside {SUBNET}")
        if client.source_ip in RESERVED_IPS:
            raise ValueError(f"{client.source_ip} collides with a reserved address")

    per_host = {
        host_id: sum(client.physical_host_id == host_id for client in clients)
        for host_id in {client.physical_host_id for client in clients}
    }
    if len(per_host) != PHYSICAL_HOST_COUNT:
        raise ValueError(f"expected {PHYSICAL_HOST_COUNT} hosts, got {len(per_host)}")
    if set(per_host.values()) != {CLIENTS_PER_HOST}:
        raise ValueError(f"unexpected clients-per-host distribution: {per_host}")


def write_csv(clients: list[LogicalClient], output_path: Path) -> None:
    rows = [asdict(client) for client in clients]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(clients: list[LogicalClient], output_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network": {
            "subnet": str(SUBNET),
            "gateway": str(GATEWAY),
            "logical_client_range": "192.168.0.151-192.168.0.250",
            "physical_host_range": "192.168.0.131-192.168.0.140",
        },
        "clients": [asdict(client) for client in clients],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def compose_text(host_id: str, clients: list[LogicalClient]) -> str:
    lines = [
        f"name: ott-clients-{host_id}",
        "",
        "services:",
    ]
    for client in clients:
        lines.extend(
            [
                f"  {client.logical_client_id}:",
                "    image: ${LOGICAL_CLIENT_IMAGE:-ott-logical-client:tnsm}",
                f"    container_name: ott-{client.logical_client_id}",
                f"    hostname: {client.logical_client_id}",
                "    environment:",
                f"      LOGICAL_CLIENT_ID: {client.logical_client_id}",
                f"      PHYSICAL_HOST_ID: {client.physical_host_id}",
                f"      PHYSICAL_HOST_IP: {client.physical_host_ip}",
                f"      SOURCE_IP: {client.source_ip}",
                f"      ACCOUNT_KEY: {client.account_key}",
                f"      ACCOUNT_EMAIL: {client.account_email}",
                '      ACCOUNT_PASSWORD: "${OTT_TEST_PASSWORD:?OTT_TEST_PASSWORD is required}"',
                f"      DEVICE_ID: {client.device_id}",
                f"      EDGE_ID: {client.edge_id}",
                f"      EDGE_BASE_URL: {client.edge_base_url}",
                f"      NETWORK_PROFILE_ID: {client.network_profile_id}",
                "    cap_add:",
                "      - NET_ADMIN",
                "    networks:",
                "      logical_clients:",
                f"        ipv4_address: {client.source_ip}",
                "    restart: unless-stopped",
                "    labels:",
                f"      ott.logical_client_id: {client.logical_client_id}",
                f"      ott.physical_host_id: {client.physical_host_id}",
                f"      ott.network_profile_id: {client.network_profile_id}",
                "",
            ]
        )

    lines.extend(
        [
            "networks:",
            "  logical_clients:",
            f"    name: ott-logical-clients-{host_id}",
            "    driver: ipvlan",
            "    driver_opts:",
            "      parent: ${CLIENT_PARENT_INTERFACE:-eth0}",
            "      ipvlan_mode: l2",
            "    ipam:",
            "      config:",
            f"        - subnet: {SUBNET}",
            f"          gateway: {GATEWAY}",
            "",
        ]
    )
    return "\n".join(lines)


def write_host_compose_files(clients: list[LogicalClient], output_dir: Path) -> None:
    host_ids = sorted({client.physical_host_id for client in clients})
    for host_id in host_ids:
        host_dir = output_dir / host_id
        host_dir.mkdir(parents=True, exist_ok=True)
        host_clients = [client for client in clients if client.physical_host_id == host_id]
        (host_dir / "docker-compose.yml").write_text(
            compose_text(host_id, host_clients),
            encoding="utf-8",
        )


def parse_args() -> argparse.Namespace:
    default_output = Path(__file__).resolve().parents[1] / "07_generated"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    clients = build_inventory()
    write_csv(clients, output_dir / "logical_clients.csv")
    write_json(clients, output_dir / "logical_clients.json")
    write_host_compose_files(clients, output_dir)
    print(f"generated {len(clients)} logical clients in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
