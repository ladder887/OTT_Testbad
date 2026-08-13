"""Validate a generated logical-client inventory before deployment."""

from __future__ import annotations

import argparse
import ipaddress
import json
from collections import Counter
from pathlib import Path


EXPECTED_CLIENTS = 100
EXPECTED_HOSTS = 10
EXPECTED_CLIENTS_PER_HOST = 10
EXPECTED_EDGES = {"edge-kr", "edge-jp", "edge-sg", "edge-us"}
EXPECTED_PROFILES = {"P0", "P1", "P2", "P3", "P4"}
LOGICAL_RANGE = ipaddress.summarize_address_range(
    ipaddress.ip_address("192.168.0.151"),
    ipaddress.ip_address("192.168.0.250"),
)
LOGICAL_IPS = {
    str(ip)
    for network in LOGICAL_RANGE
    for ip in network
    if 151 <= int(str(ip).split(".")[-1]) <= 250
}


def require_unique(clients: list[dict[str, object]], key: str) -> None:
    values = [str(client[key]) for client in clients]
    if len(values) != len(set(values)):
        duplicates = [value for value, count in Counter(values).items() if count > 1]
        raise ValueError(f"{key} contains duplicates: {duplicates}")


def validate(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    clients = payload.get("clients")
    if not isinstance(clients, list):
        raise ValueError("clients must be a list")
    if len(clients) != EXPECTED_CLIENTS:
        raise ValueError(f"expected {EXPECTED_CLIENTS} clients, got {len(clients)}")

    for key in (
        "logical_client_id",
        "source_ip",
        "account_key",
        "account_email",
        "device_id",
    ):
        require_unique(clients, key)

    source_ips = {str(client["source_ip"]) for client in clients}
    if source_ips != LOGICAL_IPS:
        missing = sorted(LOGICAL_IPS - source_ips)
        unexpected = sorted(source_ips - LOGICAL_IPS)
        raise ValueError(f"source IP range mismatch; missing={missing}, unexpected={unexpected}")

    host_counts = Counter(str(client["physical_host_id"]) for client in clients)
    if len(host_counts) != EXPECTED_HOSTS or set(host_counts.values()) != {
        EXPECTED_CLIENTS_PER_HOST
    }:
        raise ValueError(f"invalid host distribution: {dict(host_counts)}")

    edge_counts = Counter(str(client["edge_id"]) for client in clients)
    if set(edge_counts) != EXPECTED_EDGES:
        raise ValueError(f"invalid Edge set: {dict(edge_counts)}")

    profile_counts = Counter(str(client["network_profile_id"]) for client in clients)
    if set(profile_counts) != EXPECTED_PROFILES or set(profile_counts.values()) != {20}:
        raise ValueError(f"invalid network profile distribution: {dict(profile_counts)}")

    return {
        "clients": len(clients),
        "hosts": dict(sorted(host_counts.items())),
        "edges": dict(sorted(edge_counts.items())),
        "network_profiles": dict(sorted(profile_counts.items())),
        "source_ip_min": min(source_ips, key=ipaddress.ip_address),
        "source_ip_max": max(source_ips, key=ipaddress.ip_address),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = validate(args.inventory.resolve())
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
