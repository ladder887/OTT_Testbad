"""Coordinate one normal or attack scenario across logical-client containers."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = REPOSITORY_ROOT / "03_experiments" / "07_generated" / "logical_clients.json"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "06_outputs" / "01_run_manifests"
GENERATOR_PATH = Path(__file__).with_name("generate_logical_client_inventory.py")

NORMAL_SCENARIOS = {f"N{index}" for index in range(1, 8)}
MAIN_ATTACK_SCENARIOS = {"A1", "A2", "A3", "A6", "A7"}
SUPPORTED_SCENARIOS = NORMAL_SCENARIOS | MAIN_ATTACK_SCENARIOS
ATTACK_FAMILY = {"A1": "M1", "A2": "M2", "A3": "M2", "A6": "M4", "A7": "M5"}

SCENARIO_VARIANTS = {
    "N1": ("preview", "standard", "long", "catalog_preview"),
    "N2": ("default",),
    "N3": ("default",),
    "N4": ("default",),
    "N5": ("default",),
    "N6": ("household", "flash_crowd"),
    "N7": ("single", "popular_channel"),
    "A1": ("low_fanout", "high_fanout"),
    "A2": ("fast", "stealth"),
    "A3": ("low_parallel", "high_parallel"),
    "A6": ("low_rate",),
    "A7": ("low_fanout", "high_fanout"),
}
DEFAULT_VARIANTS = {
    "N1": "standard",
    "N2": "default",
    "N3": "default",
    "N4": "default",
    "N5": "default",
    "N6": "household",
    "N7": "single",
    "A1": "low_fanout",
    "A2": "stealth",
    "A3": "low_parallel",
    "A6": "low_rate",
    "A7": "low_fanout",
}

BROWSER_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 8.0) AppleWebKit/537.36 TV Safari/537.36",
)
TOOL_USER_AGENTS = (
    "python-urllib/3.12",
    "curl/8.7.1",
    "Wget/1.21.4",
)

STANDARD_VOD_CONTENT_IDS = tuple(
    content_id for content_id in (f"video_{index:02d}" for index in range(1, 16))
    if content_id not in {"video_07", "video_08"}
)
LONG_VOD_CONTENT_IDS = (
    "video_01",
    "video_04",
    "video_06",
    "video_09",
    "video_10",
    "video_13",
    "video_14",
)


class CoordinatorError(RuntimeError):
    """Raised when a remote scenario action cannot be completed."""


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_scenario_variant(scenario_id: str, requested_variant: str, seed: int) -> str:
    allowed = SCENARIO_VARIANTS[scenario_id]
    normalized = requested_variant.strip().lower().replace("-", "_") or "default"
    if normalized == "default":
        return DEFAULT_VARIANTS[scenario_id]
    if normalized == "auto":
        scenario_salt = sum((index + 1) * ord(character) for index, character in enumerate(scenario_id))
        return random.Random(seed + scenario_salt).choice(allowed)
    if normalized not in allowed:
        raise CoordinatorError(
            f"unsupported variant for {scenario_id}: {requested_variant}; allowed={','.join(allowed)}"
        )
    return normalized


@dataclass(frozen=True)
class LogicalClient:
    logical_client_id: str
    physical_host_id: str
    physical_host_ip: str
    source_ip: str
    account_email: str
    device_id: str
    edge_id: str
    edge_base_url: str
    network_profile_id: str

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "LogicalClient":
        return cls(**{field: str(row[field]) for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Assignment:
    client: LogicalClient
    spec: dict[str, Any]
    role: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_inventory(path: Path) -> list[LogicalClient]:
    if not path.exists() or path.resolve() == DEFAULT_INVENTORY.resolve():
        path.parent.mkdir(parents=True, exist_ok=True)
        spec = importlib.util.spec_from_file_location("logical_inventory_generator", GENERATOR_PATH)
        if spec is None or spec.loader is None:
            raise CoordinatorError(f"cannot load inventory generator: {GENERATOR_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        generated_clients = module.build_inventory()
        module.write_csv(generated_clients, path.parent / "logical_clients.csv")
        module.write_json(generated_clients, path)
        module.write_host_compose_files(generated_clients, path.parent)

    payload = json.loads(path.read_text(encoding="utf-8"))
    clients = [LogicalClient.from_dict(row) for row in payload.get("clients", [])]
    if len(clients) != 100:
        raise CoordinatorError(f"expected 100 logical clients, found {len(clients)}")
    return clients


def encode_spec(spec: dict[str, Any]) -> str:
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def scrub_remote_result(value: Any) -> Any:
    """Remove signed playback URLs before persisting a run report."""
    if isinstance(value, list):
        return [scrub_remote_result(item) for item in value]
    if isinstance(value, dict):
        return {
            key: scrub_remote_result(item)
            for key, item in value.items()
            if key not in {"manifest_url", "token", "sig"}
        }
    return value


class RemoteExecutor:
    def __init__(self, ssh_user: str, ssh_key: Path, timeout_sec: float) -> None:
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key.expanduser().resolve()
        self.timeout_sec = timeout_sec

    def run(self, assignment: Assignment) -> dict[str, Any]:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-i",
            str(self.ssh_key),
            f"{self.ssh_user}@{assignment.client.physical_host_ip}",
            "docker",
            "exec",
            f"ott-{assignment.client.logical_client_id}",
            "python",
            "/app/client_agent.py",
            "run-spec",
            "--spec-base64",
            encode_spec(assignment.spec),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoordinatorError(
                f"{assignment.client.logical_client_id} ({assignment.role}) exceeded {self.timeout_sec:.0f}s"
            ) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise CoordinatorError(
                f"{assignment.client.logical_client_id} ({assignment.role}) failed: {stderr or 'no error output'}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CoordinatorError(
                f"{assignment.client.logical_client_id} returned invalid JSON: {completed.stdout[:300]}"
            ) from exc
        if not result.get("ok"):
            raise CoordinatorError(
                f"{assignment.client.logical_client_id} reported a failed traffic action"
            )
        return result


def select_clients(clients: list[LogicalClient], count: int, seed: int) -> list[LogicalClient]:
    """Select clients deterministically and spread them across physical hosts."""
    if count > len(clients):
        raise CoordinatorError(f"requested {count} clients from an inventory of {len(clients)}")
    rng = random.Random(seed)
    by_host: dict[str, list[LogicalClient]] = {}
    for client in clients:
        by_host.setdefault(client.physical_host_id, []).append(client)
    host_ids = sorted(by_host)
    rng.shuffle(host_ids)
    selected: list[LogicalClient] = []
    while len(selected) < count:
        progressed = False
        for host_id in host_ids:
            candidates = [client for client in by_host[host_id] if client not in selected]
            if not candidates:
                continue
            selected.append(rng.choice(candidates))
            progressed = True
            if len(selected) == count:
                break
        if not progressed:
            break
    return selected


def choose_camouflage(rng: random.Random, content_id: str, attack: bool, offset: int = 0) -> dict[str, Any]:
    del attack
    browser_probability = 0.70
    browser = rng.random() < browser_probability
    pool = BROWSER_USER_AGENTS if browser else TOOL_USER_AGENTS
    user_agent = pool[(rng.randrange(len(pool)) + offset) % len(pool)]
    referrer_draw = rng.random()
    if referrer_draw < 0.60:
        referrer_mode = "self"
        referrer = f"http://192.168.0.101:5173/watch/{content_id}"
    elif referrer_draw < 0.85:
        referrer_mode = "empty"
        referrer = ""
    else:
        referrer_mode = "external"
        referrer = "https://example.org/"
    browse = rng.random() < 0.70
    return {
        "ua_mode": "browser" if browser else "tool",
        "user_agent": user_agent,
        "referrer_mode": referrer_mode,
        "referrer": referrer,
        "browse": browse,
    }


def common_spec(seed: int, camouflage: dict[str, Any], operation: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "seed": seed,
        "user_agent": camouflage["user_agent"],
        "referrer": camouflage["referrer"],
        "timeout_sec": 20,
        "retries": 3,
    }


def vod_phase(
    count: int,
    *,
    rendition: str = "720p",
    start_mode: str = "continue",
    start_index: int = 0,
    start_fraction: float = 0.0,
    delay: tuple[float, float] = (5.0, 6.8),
    initial_buffer_count: int = 0,
    pause_before_sec: float = 0.0,
    parallelism: int = 1,
) -> dict[str, Any]:
    return {
        "segment_count": count,
        "rendition": rendition,
        "start_mode": start_mode,
        "start_index": start_index,
        "start_fraction": start_fraction,
        "delay_min_sec": delay[0],
        "delay_max_sec": delay[1],
        "initial_buffer_count": initial_buffer_count,
        "pause_before_sec": pause_before_sec,
        "parallelism": parallelism,
    }


class ScenarioCoordinator:
    def __init__(
        self,
        clients: list[LogicalClient],
        executor: RemoteExecutor,
        scenario_id: str,
        seed: int,
        smoke: bool,
        dataset_prefix: str,
        output_dir: Path,
        cache_state: str = "unspecified",
        variant: str = "default",
    ) -> None:
        self.clients = clients
        self.executor = executor
        self.scenario_id = scenario_id
        self.seed = seed
        self.smoke = smoke
        self.dataset_prefix = dataset_prefix
        self.output_dir = output_dir
        self.rng = random.Random(seed)
        self.requested_variant = variant.strip().lower().replace("-", "_") or "default"
        self.variant = resolve_scenario_variant(scenario_id, self.requested_variant, seed)
        self.selected: list[LogicalClient] = []
        self.parameters: dict[str, Any] = {
            "collection_mode": "smoke" if smoke else "main",
            "timing_scaled": smoke,
            "cache_state": cache_state,
            "scenario_variant": self.variant,
            "requested_variant": self.requested_variant,
        }
        self.remote_results: list[dict[str, Any]] = []
        self.token_bindings: list[dict[str, Any]] = []

    def count(self, low: int, high: int, smoke_value: int) -> int:
        return smoke_value if self.smoke else self.rng.randint(low, high)

    def delay(self, normal: tuple[float, float], smoke: tuple[float, float] = (0.05, 0.12)) -> tuple[float, float]:
        return smoke if self.smoke else normal

    def pause(self, low: float, high: float, smoke_value: float) -> float:
        return smoke_value if self.smoke else self.rng.uniform(low, high)

    def content(self, live: bool = False, exclude: set[str] | None = None) -> str:
        candidates = [f"live_{index:02d}" for index in range(1, 4)] if live else [
            f"video_{index:02d}" for index in range(1, 16)
        ]
        available = [item for item in candidates if item not in (exclude or set())]
        return self.rng.choice(available)

    def _record_assignment_result(self, assignment: Assignment, result: dict[str, Any]) -> None:
        self.remote_results.append(
            {
                "logical_client_id": assignment.client.logical_client_id,
                "role": assignment.role,
                **scrub_remote_result(result),
            }
        )

    def _record_playback_bindings(
        self,
        result: dict[str, Any],
        owner: LogicalClient,
        consumers: list[LogicalClient] | None = None,
    ) -> None:
        consumer_ids = [item.logical_client_id for item in (consumers or [owner])]
        for playback in result.get("playbacks", []):
            binding = dict(playback["token_binding"])
            binding["owner_logical_client_id"] = owner.logical_client_id
            binding["consumer_logical_client_ids"] = consumer_ids
            self.token_bindings.append(binding)

    def run_one(self, assignment: Assignment) -> dict[str, Any]:
        result = self.executor.run(assignment)
        self._record_assignment_result(assignment, result)
        return result

    def run_parallel(self, assignments: list[Assignment]) -> list[dict[str, Any]]:
        results_by_id: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(assignments)) as pool:
            futures = {pool.submit(self.executor.run, assignment): assignment for assignment in assignments}
            for future in concurrent.futures.as_completed(futures):
                assignment = futures[future]
                result = future.result()
                results_by_id[assignment.client.logical_client_id] = result
                self._record_assignment_result(assignment, result)
        return [results_by_id[item.client.logical_client_id] for item in assignments]

    def single_vod(self, phases: list[dict[str, Any]], content_id: str | None = None) -> None:
        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_id = content_id or self.content()
        camouflage = choose_camouflage(self.rng, content_id, attack=False)
        spec = {
            **common_spec(self.seed, camouflage, "vod"),
            "content_id": content_id,
            "browse": camouflage["browse"],
            "phases": phases,
        }
        result = self.run_one(Assignment(client, spec, "viewer"))
        self._record_playback_bindings(result, client)
        self.parameters.update({"content_id": content_id, "camouflage": camouflage, "phases": phases})

    def run_n1(self) -> None:
        if self.variant == "catalog_preview":
            self._run_n1_catalog_preview()
            return

        profile_ranges = {
            "preview": (5, 15),
            "standard": (30, 75),
            "long": (90, 200),
        }
        low, high = profile_ranges[self.variant]
        if self.variant == "long":
            content_id = self.rng.choice(LONG_VOD_CONTENT_IDS)
        elif self.variant == "standard":
            content_id = self.rng.choice(STANDARD_VOD_CONTENT_IDS)
        else:
            content_id = self.content()
        phase = vod_phase(
            self.count(low, high, 5),
            delay=self.delay((5.2, 6.9)),
            initial_buffer_count=3,
        )
        self.single_vod([phase], content_id=content_id)
        self.parameters["viewing_profile"] = self.variant

    def _run_n1_catalog_preview(self) -> None:
        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_count = self.count(2, 5, 2)
        content_ids: list[str] = []
        while len(content_ids) < content_count:
            content_ids.append(self.content(exclude=set(content_ids)))

        camouflage = choose_camouflage(self.rng, content_ids[0], attack=False)
        flows: list[dict[str, Any]] = []
        for index, content_id in enumerate(content_ids):
            flow = {
                "content_id": content_id,
                "browse": True if index else camouflage["browse"],
                "phases": [
                    vod_phase(
                        self.count(5, 15, 3),
                        delay=self.delay((5.0, 6.8)),
                        initial_buffer_count=2,
                    )
                ],
            }
            if index:
                flow["pause_before_sec"] = self.pause(8.0, 45.0, 0.2)
            flows.append(flow)

        spec = {**common_spec(self.seed, camouflage, "multi_vod"), "flows": flows}
        result = self.run_one(Assignment(client, spec, "catalog_preview_viewer"))
        self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "content_ids": content_ids,
                "content_count": content_count,
                "viewing_profile": "catalog_preview",
                "concurrency": 1,
                "shared_token": False,
                "camouflage": camouflage,
                "flows": flows,
            }
        )

    def run_n2(self) -> None:
        before = self.count(15, 25, 3)
        after = self.count(15, 25, 3)
        seek_fraction = self.rng.uniform(0.20, 0.70)
        phases = [
            vod_phase(before, delay=self.delay((5.0, 6.7)), initial_buffer_count=2),
            vod_phase(
                after,
                start_mode="fraction",
                start_fraction=seek_fraction,
                delay=self.delay((5.0, 6.7)),
                pause_before_sec=0.2 if self.smoke else 2.0,
            ),
        ]
        self.single_vod(phases)
        self.parameters["seek_fraction"] = round(seek_fraction, 4)

    def run_n3(self) -> None:
        first = self.count(10, 18, 3)
        second = self.count(10, 18, 3)
        phases = [
            vod_phase(first, rendition="1080p", delay=self.delay((5.0, 6.6)), initial_buffer_count=2),
            vod_phase(
                second,
                rendition="720p",
                delay=self.delay((5.0, 6.6)),
                pause_before_sec=0.2 if self.smoke else 2.0,
            ),
        ]
        self.single_vod(phases)
        self.parameters["switch_type"] = "manual_non_overlapping"

    def run_n4(self) -> None:
        pause_sec = self.pause(15, 90, 1.0)
        phases = [
            vod_phase(self.count(15, 25, 3), delay=self.delay((5.2, 6.8)), initial_buffer_count=2),
            vod_phase(
                self.count(15, 25, 3),
                delay=self.delay((5.2, 6.8)),
                pause_before_sec=pause_sec,
            ),
        ]
        self.single_vod(phases)
        self.parameters["pause_sec"] = round(pause_sec, 3)

    def run_n5(self) -> None:
        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_id = self.content()
        pause_sec = self.pause(180, 600, 3.0)
        camouflage = choose_camouflage(self.rng, content_id, attack=False)
        flow_one = {
            "content_id": content_id,
            "browse": camouflage["browse"],
            "phases": [
                vod_phase(self.count(15, 30, 3), delay=self.delay((5.2, 6.8)), initial_buffer_count=2)
            ],
        }
        flow_two = {
            "content_id": content_id,
            "browse": camouflage["browse"],
            "pause_before_sec": pause_sec,
            "phases": [
                vod_phase(self.count(15, 30, 3), delay=self.delay((5.2, 6.8)), initial_buffer_count=2)
            ],
        }
        spec = {**common_spec(self.seed, camouflage, "multi_vod"), "flows": [flow_one, flow_two]}
        result = self.run_one(Assignment(client, spec, "returning_viewer"))
        self._record_playback_bindings(result, client)
        self.parameters.update(
            {"content_id": content_id, "camouflage": camouflage, "pause_sec": round(pause_sec, 3), "flows": [flow_one, flow_two]}
        )

    def run_n6(self) -> None:
        if self.variant == "flash_crowd":
            self._run_n6_flash_crowd()
            return

        consumer_count = self.count(2, 4, 2)
        selected = select_clients(self.clients, consumer_count, self.seed)
        self.selected = selected
        household_email = selected[0].account_email
        assignments: list[Assignment] = []
        member_parameters: list[dict[str, Any]] = []
        used_contents: set[str] = set()
        for index, client in enumerate(selected):
            content_id = self.content(exclude=used_contents)
            used_contents.add(content_id)
            camouflage = choose_camouflage(self.rng, content_id, attack=False, offset=index)
            phase = vod_phase(
                self.count(30, 50, 4),
                delay=self.delay((5.1, 6.7)),
                initial_buffer_count=2,
            )
            spec = {
                **common_spec(self.seed + index, camouflage, "vod"),
                "account_email": household_email,
                "content_id": content_id,
                "browse": camouflage["browse"],
                "phases": [phase],
            }
            assignments.append(Assignment(client, spec, "household_member"))
            member_parameters.append(
                {
                    "logical_client_id": client.logical_client_id,
                    "content_id": content_id,
                    "camouflage": camouflage,
                    "phase": phase,
                }
            )
        results = self.run_parallel(assignments)
        for client, result in zip(selected, results):
            self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "consumer_count": consumer_count,
                "household_account_owner": selected[0].logical_client_id,
                "members": member_parameters,
                "shared_account": True,
                "shared_token": False,
                "shared_content": False,
                "actual_account_count": 1,
                "actual_token_count": len(self.token_bindings),
            }
        )

    def _run_n6_flash_crowd(self) -> None:
        consumer_count = self.count(2, 5, 2)
        selected = select_clients(self.clients, consumer_count, self.seed)
        self.selected = selected
        content_id = self.content()
        assignments: list[Assignment] = []
        viewer_parameters: list[dict[str, Any]] = []
        for index, client in enumerate(selected):
            camouflage = choose_camouflage(self.rng, content_id, attack=False, offset=index)
            phase = vod_phase(
                self.count(30, 50, 4),
                delay=self.delay((5.1, 6.7)),
                initial_buffer_count=2,
            )
            spec = {
                **common_spec(self.seed + index, camouflage, "vod"),
                "content_id": content_id,
                "browse": camouflage["browse"],
                "phases": [phase],
            }
            assignments.append(Assignment(client, spec, "flash_crowd_viewer"))
            viewer_parameters.append(
                {
                    "logical_client_id": client.logical_client_id,
                    "content_id": content_id,
                    "camouflage": camouflage,
                    "phase": phase,
                }
            )

        results = self.run_parallel(assignments)
        for client, result in zip(selected, results):
            self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "content_id": content_id,
                "consumer_count": consumer_count,
                "viewers": viewer_parameters,
                "shared_account": False,
                "shared_token": False,
                "shared_content": True,
                "actual_account_count": consumer_count,
                "actual_token_count": len(self.token_bindings),
            }
        )

    def run_n7(self) -> None:
        if self.variant == "popular_channel":
            self._run_n7_popular_channel()
            return

        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_id = self.content(live=True)
        duration_sec = self.pause(220, 420, 15.0)
        camouflage = choose_camouflage(self.rng, content_id, attack=False)
        live = {"duration_sec": duration_sec, "rendition": "720p", "poll_factor": 1.0, "initial_segments": 2}
        spec = {
            **common_spec(self.seed, camouflage, "live"),
            "content_id": content_id,
            "browse": camouflage["browse"],
            "live": live,
        }
        result = self.run_one(Assignment(client, spec, "live_viewer"))
        self._record_playback_bindings(result, client)
        if not result.get("traffic", {}).get("rolling_playlist"):
            raise CoordinatorError("N7 LIVE playlist did not advance during the run")
        self.parameters.update(
            {
                "content_id": content_id,
                "consumer_count": 1,
                "camouflage": camouflage,
                "live": live,
                "shared_token": False,
                "actual_token_count": 1,
            }
        )

    def _run_n7_popular_channel(self) -> None:
        consumer_count = self.count(2, 5, 2)
        selected = select_clients(self.clients, consumer_count, self.seed)
        self.selected = selected
        content_id = self.content(live=True)
        duration_sec = self.pause(220, 420, 15.0)
        assignments: list[Assignment] = []
        viewer_parameters: list[dict[str, Any]] = []
        for index, client in enumerate(selected):
            camouflage = choose_camouflage(self.rng, content_id, attack=False, offset=index)
            live = {
                "duration_sec": duration_sec,
                "rendition": "720p",
                "poll_factor": 1.0,
                "initial_segments": 2,
            }
            spec = {
                **common_spec(self.seed + index, camouflage, "live"),
                "content_id": content_id,
                "browse": camouflage["browse"],
                "live": live,
            }
            assignments.append(Assignment(client, spec, "popular_live_viewer"))
            viewer_parameters.append(
                {
                    "logical_client_id": client.logical_client_id,
                    "camouflage": camouflage,
                    "live": live,
                }
            )

        results = self.run_parallel(assignments)
        if any(not result.get("traffic", {}).get("rolling_playlist") for result in results):
            raise CoordinatorError("N7 popular-channel LIVE playlist did not advance for every viewer")
        for client, result in zip(selected, results):
            self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "content_id": content_id,
                "consumer_count": consumer_count,
                "viewers": viewer_parameters,
                "live": {
                    "duration_sec": duration_sec,
                    "rendition": "720p",
                    "poll_factor": 1.0,
                    "initial_segments": 2,
                },
                "shared_account": False,
                "shared_token": False,
                "shared_content": True,
                "actual_account_count": consumer_count,
                "actual_token_count": len(self.token_bindings),
            }
        )

    def _run_token_relay(self, live: bool) -> None:
        consumer_count = 2 if self.variant == "low_fanout" else self.count(3, 5, 3)
        selected = select_clients(self.clients, consumer_count + 1, self.seed)
        owner = selected[0]
        consumers = selected[1:]
        self.selected = selected
        content_id = self.content(live=live)
        owner_camouflage = choose_camouflage(self.rng, content_id, attack=True)
        issue_spec = {
            **common_spec(self.seed, owner_camouflage, "issue"),
            "content_id": content_id,
            "browse": owner_camouflage["browse"],
        }
        issue_result = self.run_one(Assignment(owner, issue_spec, "token_owner"))
        playback = issue_result["playbacks"][0]
        manifest_url = playback["manifest_url"]
        assignments: list[Assignment] = []
        consumer_parameters: list[dict[str, Any]] = []
        for index, consumer in enumerate(consumers):
            camouflage = choose_camouflage(self.rng, content_id, attack=True, offset=index + 1)
            if live:
                duration_sec = self.pause(220, 420, 15.0)
                traffic_spec = {
                    "operation": "consume_live",
                    "manifest_url": manifest_url,
                    "live": {
                        "duration_sec": duration_sec,
                        "rendition": "720p",
                        "poll_factor": 1.0,
                        "initial_segments": 2,
                    },
                }
            else:
                count = self.count(18, 42, 4)
                traffic_spec = {
                    "operation": "consume_vod",
                    "manifest_url": manifest_url,
                    "phases": [
                        vod_phase(
                            count,
                            start_mode="absolute",
                            start_index=index * max(1, count // 2),
                            delay=self.delay((5.0, 6.8)),
                            initial_buffer_count=1,
                        )
                    ],
                }
            spec = {
                **common_spec(self.seed + index + 1, camouflage, traffic_spec.pop("operation")),
                **traffic_spec,
            }
            assignments.append(Assignment(consumer, spec, "relay_consumer"))
            consumer_parameters.append(
                {
                    "logical_client_id": consumer.logical_client_id,
                    "camouflage": camouflage,
                    "traffic": scrub_remote_result(traffic_spec),
                }
            )
        results = self.run_parallel(assignments)
        if live and any(not result.get("traffic", {}).get("rolling_playlist") for result in results):
            raise CoordinatorError("A7 requires a rolling LIVE playlist for every consumer")
        self._record_playback_bindings(issue_result, owner, consumers)
        self.parameters.update(
            {
                "content_id": content_id,
                "consumer_count": consumer_count,
                "fanout_variant": self.variant,
                "owner_logical_client_id": owner.logical_client_id,
                "owner_camouflage": owner_camouflage,
                "consumers": consumer_parameters,
                "shared_token": True,
                "request_edge_id": owner.edge_id,
            }
        )

    def run_a1(self) -> None:
        self._run_token_relay(live=False)

    def run_a2(self) -> None:
        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_count = self.count(2, 5, 2)
        content_ids: list[str] = []
        while len(content_ids) < content_count:
            candidate = self.content(exclude=set(content_ids))
            content_ids.append(candidate)
        camouflage = choose_camouflage(self.rng, content_ids[0], attack=True)
        variant = self.variant
        delay = self.delay((0.4, 1.5) if variant == "fast" else (2.5, 5.5))
        flows = []
        for index, content_id in enumerate(content_ids):
            flows.append(
                {
                    "content_id": content_id,
                    "browse": camouflage["browse"] if index == 0 else False,
                    "pause_before_sec": 0.2 if self.smoke else self.rng.uniform(1.0, 4.0),
                    "phases": [
                        vod_phase(
                            self.count(22, 50, 4),
                            delay=delay,
                            initial_buffer_count=1,
                        )
                    ],
                }
            )
        spec = {**common_spec(self.seed, camouflage, "multi_vod"), "flows": flows}
        result = self.run_one(Assignment(client, spec, "serial_harvester"))
        self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "content_ids": content_ids,
                "content_count": content_count,
                "download_variant": variant,
                "concurrency": 1,
                "camouflage": camouflage,
                "flows": flows,
            }
        )

    def run_a3(self) -> None:
        client = select_clients(self.clients, 1, self.seed)[0]
        self.selected = [client]
        content_id = self.content()
        if self.variant == "low_parallel":
            workers = 2
            segment_count = self.count(32, 50, 6)
        else:
            workers = self.count(3, 4, 3)
            segment_count = self.count(51, 76, 8)
        camouflage = choose_camouflage(self.rng, content_id, attack=True)
        phase = vod_phase(
            segment_count,
            delay=self.delay((2.0, 5.0)),
            parallelism=workers,
        )
        spec = {
            **common_spec(self.seed, camouflage, "vod"),
            "content_id": content_id,
            "browse": camouflage["browse"],
            "phases": [phase],
        }
        result = self.run_one(Assignment(client, spec, "parallel_harvester"))
        self._record_playback_bindings(result, client)
        self.parameters.update(
            {
                "content_id": content_id,
                "worker_count": workers,
                "parallel_variant": self.variant,
                "range_overlap_ratio": 0.0,
                "camouflage": camouflage,
                "phase": phase,
            }
        )

    def run_a6(self) -> None:
        selected = select_clients(self.clients, 4, self.seed)
        self.selected = selected
        content_id = self.content()
        hop_count = self.count(7, 17, 2)
        handoff_delays: list[float] = []
        hop_parameters: list[dict[str, Any]] = []
        for index, client in enumerate(selected):
            if index:
                handoff = self.pause(3, 8, 0.3)
                handoff_delays.append(handoff)
                time.sleep(handoff)
            camouflage = choose_camouflage(self.rng, content_id, attack=True, offset=index)
            phase = vod_phase(
                hop_count,
                start_mode="absolute",
                start_index=index * hop_count,
                delay=self.delay((5.0, 6.8)),
            )
            spec = {
                **common_spec(self.seed + index, camouflage, "vod"),
                "content_id": content_id,
                "browse": camouflage["browse"],
                "phases": [phase],
            }
            assignment = Assignment(client, spec, f"hop_{index + 1}")
            result = self.run_one(assignment)
            self._record_playback_bindings(result, client)
            hop_parameters.append(
                {
                    "hop": index + 1,
                    "logical_client_id": client.logical_client_id,
                    "account_owner": client.logical_client_id,
                    "camouflage": camouflage,
                    "phase": phase,
                }
            )
        self.parameters.update(
            {
                "content_id": content_id,
                "participant_count": 4,
                "actual_account_count": 4,
                "actual_token_count": 4,
                "handoff_delays_sec": [round(item, 3) for item in handoff_delays],
                "hops": hop_parameters,
            }
        )

    def run_a7(self) -> None:
        self._run_token_relay(live=True)

    def execute(self, dry_run: bool = False) -> tuple[dict[str, Any], Path]:
        run_method: Callable[[], None] = getattr(self, f"run_{self.scenario_id.lower()}")
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{self.scenario_id.lower()}_{uuid.uuid4().hex[:8]}"
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "dataset_prefix": self.dataset_prefix,
            "scenario_id": self.scenario_id,
            "attack_family": ATTACK_FAMILY.get(self.scenario_id),
            "seed": self.seed,
            "status": "scheduled" if dry_run else "running",
            "logical_client_ids": [],
            "parameters": self.parameters,
            "token_bindings": [],
            "scheduled_at": utc_now(),
            "started_at": None if dry_run else utc_now(),
            "ended_at": None,
            "expected_request_count": None,
            "observed_request_count": None,
            "error": None,
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.output_dir / f"{run_id}.json"

        if dry_run:
            if self.scenario_id in {"A1", "A7"}:
                preview_count = 3 if self.variant == "low_fanout" else 6
            elif self.scenario_id == "N6":
                preview_count = 5 if self.variant == "flash_crowd" else 4
            elif self.scenario_id == "N7" and self.variant == "popular_channel":
                preview_count = 5
            elif self.scenario_id == "A6":
                preview_count = 4
            else:
                preview_count = 1
            self.selected = select_clients(self.clients, preview_count, self.seed)
            manifest["logical_client_ids"] = [item.logical_client_id for item in self.selected]
            manifest["parameters"] = {
                **self.parameters,
                "dry_run": True,
                "selected_clients": [
                    {
                        "logical_client_id": item.logical_client_id,
                        "physical_host_id": item.physical_host_id,
                        "source_ip": item.source_ip,
                        "edge_id": item.edge_id,
                    }
                    for item in self.selected
                ],
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return manifest, manifest_path

        try:
            run_method()
            manifest["status"] = "completed"
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["error"] = str(exc)
            raise
        finally:
            manifest["logical_client_ids"] = [item.logical_client_id for item in self.selected]
            manifest["parameters"] = {
                **self.parameters,
                "selected_clients": [
                    {
                        "logical_client_id": item.logical_client_id,
                        "physical_host_id": item.physical_host_id,
                        "source_ip": item.source_ip,
                        "edge_id": item.edge_id,
                        "network_profile_id": item.network_profile_id,
                    }
                    for item in self.selected
                ],
                "client_results": self.remote_results,
            }
            manifest["token_bindings"] = self.token_bindings
            manifest["ended_at"] = utc_now()
            observed = sum(_safe_int(item.get("http_request_count"), 0) for item in self.remote_results)
            expected = sum(
                max(
                    0,
                    _safe_int(item.get("http_request_count"), 0)
                    - _safe_int(item.get("http_retry_count"), 0),
                )
                for item in self.remote_results
            )
            manifest["observed_request_count"] = observed
            manifest["expected_request_count"] = expected if manifest["status"] == "completed" else None
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest, manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="N1-N7 or A1/A2/A3/A6/A7")
    parser.add_argument(
        "--variant",
        default="default",
        help="scenario-specific variant; use 'auto' for deterministic seed-based selection",
    )
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--smoke", action="store_true", help="scale counts and delays for pipeline verification only")
    parser.add_argument("--dry-run", action="store_true", help="select clients and write a scheduled manifest without traffic")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dataset-prefix", default="")
    parser.add_argument("--ssh-user", default="ottadmin")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "ott_lab_ed25519")
    parser.add_argument("--remote-timeout-sec", type=float, default=1800.0)
    parser.add_argument(
        "--cache-state",
        choices=("unspecified", "cold", "warmup", "warm", "mixed"),
        default="unspecified",
        help="record the externally prepared Edge cache condition in the run manifest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scenario_id = args.scenario.strip().upper()
    if scenario_id in {"A4", "A5"}:
        reason = (
            "A4 is disabled because the deployed platform has only 1080p and 720p; add a third rendition first."
            if scenario_id == "A4"
            else "A5 is excluded from the main ViewingSession task; implement it as a separate Account-TimeWindow study."
        )
        print(reason, file=sys.stderr)
        return 2
    if scenario_id not in SUPPORTED_SCENARIOS:
        print(f"unsupported scenario: {scenario_id}; supported={','.join(sorted(SUPPORTED_SCENARIOS))}", file=sys.stderr)
        return 2
    try:
        resolve_scenario_variant(scenario_id, args.variant, args.seed)
    except CoordinatorError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    dataset_prefix = args.dataset_prefix.strip() or (
        f"tnsm_100lc_{datetime.now(timezone.utc):%Y%m%d}_{'smoke' if args.smoke else 'main'}"
    )
    clients = load_inventory(args.inventory.resolve())
    executor = RemoteExecutor(args.ssh_user, args.ssh_key, args.remote_timeout_sec)
    coordinator = ScenarioCoordinator(
        clients=clients,
        executor=executor,
        scenario_id=scenario_id,
        seed=args.seed,
        smoke=args.smoke,
        dataset_prefix=dataset_prefix,
        output_dir=args.output_dir.resolve(),
        cache_state=args.cache_state,
        variant=args.variant,
    )
    try:
        manifest, path = coordinator.execute(dry_run=args.dry_run)
    except Exception as exc:
        print(f"scenario failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "scenario_id": manifest["scenario_id"],
                "scenario_variant": manifest["parameters"]["scenario_variant"],
                "status": manifest["status"],
                "logical_client_ids": manifest["logical_client_ids"],
                "observed_request_count": manifest["observed_request_count"],
                "manifest_path": str(path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
