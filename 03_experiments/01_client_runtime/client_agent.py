"""Minimal runtime used to verify logical-client identity and network reachability."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ClientConfig:
    logical_client_id: str
    physical_host_id: str
    physical_host_ip: str
    source_ip: str
    account_key: str
    account_email: str
    device_id: str
    edge_id: str
    edge_base_url: str
    network_profile_id: str

    @classmethod
    def from_environment(cls) -> "ClientConfig":
        fields = {
            "logical_client_id": "LOGICAL_CLIENT_ID",
            "physical_host_id": "PHYSICAL_HOST_ID",
            "physical_host_ip": "PHYSICAL_HOST_IP",
            "source_ip": "SOURCE_IP",
            "account_key": "ACCOUNT_KEY",
            "account_email": "ACCOUNT_EMAIL",
            "device_id": "DEVICE_ID",
            "edge_id": "EDGE_ID",
            "edge_base_url": "EDGE_BASE_URL",
            "network_profile_id": "NETWORK_PROFILE_ID",
        }
        values = {name: os.getenv(env_name, "").strip() for name, env_name in fields.items()}
        missing = [fields[name] for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(missing)}")
        return cls(**values)


def print_config(config: ClientConfig) -> int:
    print(json.dumps(asdict(config), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def probe(config: ClientConfig, timeout: float, retries: int) -> int:
    query = urllib.parse.urlencode(
        {
            "probe": "logical-client",
            "logical_client_id": config.logical_client_id,
            "physical_host_id": config.physical_host_id,
        }
    )
    # The gateway health location disables access logging. Probe the logged root
    # location so Edge.client_ip can be compared with the inventory.
    url = f"{config.edge_base_url.rstrip('/')}/?{query}"
    headers = {
        "User-Agent": f"OTT-TNSM-Probe/1.0 ({config.logical_client_id})",
        "X-Logical-Client-ID": config.logical_client_id,
        "X-Physical-Host-ID": config.physical_host_id,
        "X-Device-ID": config.device_id,
        "X-Network-Profile-ID": config.network_profile_id,
    }

    last_error = ""
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                result = {
                    "logical_client_id": config.logical_client_id,
                    "configured_source_ip": config.source_ip,
                    "edge_id": config.edge_id,
                    "url": url,
                    "status": response.status,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "response": body,
                }
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(min(attempt, 3))

    print(
        json.dumps(
            {
                "logical_client_id": config.logical_client_id,
                "configured_source_ip": config.source_ip,
                "edge_id": config.edge_id,
                "url": url,
                "error": last_error,
                "attempts": retries,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1


def idle(config: ClientConfig) -> int:
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(
        json.dumps(
            {
                "status": "idle",
                "logical_client_id": config.logical_client_id,
                "source_ip": config.source_ip,
                "edge_id": config.edge_id,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    while not stop:
        time.sleep(1)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show-config", help="print non-secret logical-client settings")
    subparsers.add_parser("idle", help="stay alive until the container is stopped")
    probe_parser = subparsers.add_parser("probe", help="request the assigned Edge health endpoint")
    probe_parser.add_argument("--timeout", type=float, default=10.0)
    probe_parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = ClientConfig.from_environment()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "show-config":
        return print_config(config)
    if args.command == "probe":
        return probe(config, timeout=args.timeout, retries=args.retries)
    return idle(config)


if __name__ == "__main__":
    raise SystemExit(main())
