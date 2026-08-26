"""Logical-client runtime for OTT playback experiments.

The agent intentionally sends no run, scenario, label, physical-host, or
logical-client metadata over HTTP. Experiment provenance stays in the run
manifest maintained by the coordinator.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import http.cookiejar
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_RETRIES = 3
SIGNED_QUERY_KEYS = ("token", "sig")


@dataclass(frozen=True)
class NetworkProfile:
    profile_id: str
    target_rtt_ms: float
    one_way_delay_ms: float
    one_way_jitter_ms: float
    one_way_loss_percent: float


NETWORK_PROFILES = {
    "P0": NetworkProfile("P0", 0.0, 0.0, 0.0, 0.0),
    "P1": NetworkProfile("P1", 18.0, 9.0, 2.5, 0.0),
    "P2": NetworkProfile("P2", 45.0, 22.5, 9.0, 0.15),
    "P3": NetworkProfile("P3", 90.0, 45.0, 14.0, 0.50),
    "P4": NetworkProfile("P4", 170.0, 85.0, 18.0, 0.25),
}


@dataclass(frozen=True)
class ClientConfig:
    logical_client_id: str
    physical_host_id: str
    physical_host_ip: str
    source_ip: str
    account_key: str
    account_email: str
    account_password: str
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
            "account_password": "ACCOUNT_PASSWORD",
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


@dataclass(frozen=True)
class HlsVariant:
    url: str
    bandwidth: int
    width: int
    height: int


@dataclass(frozen=True)
class HlsSegment:
    sequence: int
    duration_sec: float
    url: str


@dataclass(frozen=True)
class MediaPlaylist:
    url: str
    target_duration_sec: float
    media_sequence: int
    end_list: bool
    segments: list[HlsSegment]


class AgentError(RuntimeError):
    """Raised for a failed experiment action."""


def _format_netem_number(value: float) -> str:
    return f"{value:g}"


def network_profile_commands(profile_id: str, interface: str = "eth0") -> list[list[str]]:
    profile = NETWORK_PROFILES.get(profile_id.upper())
    if profile is None:
        raise ValueError(f"unknown network profile: {profile_id}")
    if profile.profile_id == "P0":
        return []
    netem = [
        "netem",
        "delay",
        f"{_format_netem_number(profile.one_way_delay_ms)}ms",
        f"{_format_netem_number(profile.one_way_jitter_ms)}ms",
        "distribution",
        "normal",
    ]
    if profile.one_way_loss_percent > 0:
        netem.extend(
            ["loss", f"{_format_netem_number(profile.one_way_loss_percent)}%"]
        )
    return [
        ["ip", "link", "add", "ifb0", "type", "ifb"],
        ["ip", "link", "set", "dev", "ifb0", "up"],
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:", *netem],
        ["tc", "qdisc", "add", "dev", interface, "handle", "ffff:", "ingress"],
        [
            "tc",
            "filter",
            "add",
            "dev",
            interface,
            "parent",
            "ffff:",
            "protocol",
            "all",
            "u32",
            "match",
            "u32",
            "0",
            "0",
            "action",
            "mirred",
            "egress",
            "redirect",
            "dev",
            "ifb0",
        ],
        ["tc", "qdisc", "add", "dev", "ifb0", "root", "handle", "1:", *netem],
    ]


def _run_network_command(command: list[str], *, ignore_failure: bool = False) -> None:
    executable = shutil.which(command[0])
    if not executable:
        if ignore_failure:
            return
        raise AgentError(f"network profile tool is missing: {command[0]}")
    completed = subprocess.run(
        [executable, *command[1:]],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0 and not ignore_failure:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise AgentError(f"network profile command failed ({' '.join(command)}): {detail}")


def configure_network_profile(config: ClientConfig, interface: str = "eth0") -> dict[str, Any]:
    profile_id = config.network_profile_id.upper()
    profile = NETWORK_PROFILES.get(profile_id)
    if profile is None:
        raise AgentError(f"unknown NETWORK_PROFILE_ID: {config.network_profile_id}")

    cleanup_commands = [
        ["tc", "qdisc", "del", "dev", interface, "root"],
        ["tc", "qdisc", "del", "dev", interface, "ingress"],
        ["ip", "link", "del", "ifb0"],
    ]
    for command in cleanup_commands:
        _run_network_command(command, ignore_failure=True)
    for command in network_profile_commands(profile_id, interface):
        _run_network_command(command)

    return {
        "profile_id": profile.profile_id,
        "mode": "baseline" if profile.profile_id == "P0" else "bidirectional_netem",
        "target_rtt_ms": profile.target_rtt_ms,
        "one_way_delay_ms": profile.one_way_delay_ms,
        "one_way_jitter_ms": profile.one_way_jitter_ms,
        "one_way_loss_percent": profile.one_way_loss_percent,
    }


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def signed_child_url(parent_url: str, child_url: str) -> str:
    """Resolve a playlist child and inherit only the CDN signature query."""
    absolute = urllib.parse.urljoin(parent_url, child_url.strip())
    parent = urllib.parse.urlsplit(parent_url)
    child = urllib.parse.urlsplit(absolute)
    parent_query = dict(urllib.parse.parse_qsl(parent.query, keep_blank_values=True))
    child_query = dict(urllib.parse.parse_qsl(child.query, keep_blank_values=True))
    for key in SIGNED_QUERY_KEYS:
        if key in parent_query and key not in child_query:
            child_query[key] = parent_query[key]
    return urllib.parse.urlunsplit(
        (child.scheme, child.netloc, child.path, urllib.parse.urlencode(child_query), child.fragment)
    )


def parse_master_playlist(text: str, master_url: str) -> list[HlsVariant]:
    variants: list[HlsVariant] = []
    pending: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes: dict[str, str] = {}
            for key, value in re.findall(r"([A-Z0-9-]+)=([^,]+)", line.split(":", 1)[1]):
                attributes[key] = value.strip().strip('"')
            pending = attributes
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        width = 0
        height = 0
        resolution = pending.get("RESOLUTION", "")
        if "x" in resolution.lower():
            width_text, height_text = resolution.lower().split("x", 1)
            width = _safe_int(width_text, 0)
            height = _safe_int(height_text, 0)
        variants.append(
            HlsVariant(
                url=signed_child_url(master_url, line),
                bandwidth=_safe_int(pending.get("BANDWIDTH"), 0),
                width=width,
                height=height,
            )
        )
        pending = None
    return variants


def parse_media_playlist(text: str, playlist_url: str) -> MediaPlaylist:
    target_duration = 6.0
    media_sequence = 0
    next_duration = 0.0
    segments: list[HlsSegment] = []
    end_list = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = _safe_float(line.split(":", 1)[1], target_duration)
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = _safe_int(line.split(":", 1)[1], media_sequence)
        elif line.startswith("#EXTINF:"):
            next_duration = _safe_float(line.split(":", 1)[1].split(",", 1)[0], target_duration)
        elif line == "#EXT-X-ENDLIST":
            end_list = True
        elif line and not line.startswith("#"):
            segments.append(
                HlsSegment(
                    sequence=media_sequence + len(segments),
                    duration_sec=next_duration or target_duration,
                    url=signed_child_url(playlist_url, line),
                )
            )
            next_duration = 0.0
    return MediaPlaylist(
        url=playlist_url,
        target_duration_sec=target_duration,
        media_sequence=media_sequence,
        end_list=end_list,
        segments=segments,
    )


def choose_variant(variants: list[HlsVariant], rendition: str) -> HlsVariant:
    if not variants:
        raise AgentError("master playlist contains no HLS variants")
    normalized = str(rendition or "auto").strip().lower()
    if normalized in {"auto", "highest"}:
        return max(variants, key=lambda item: (item.height, item.bandwidth))
    if normalized == "lowest":
        return min(variants, key=lambda item: (item.height or 100000, item.bandwidth or 100000))
    match = re.search(r"(\d{3,4})", normalized)
    if match:
        target_height = int(match.group(1))
        exact = [item for item in variants if item.height == target_height]
        if exact:
            return exact[0]
        by_path = [item for item in variants if f"{target_height}p" in item.url.lower()]
        if by_path:
            return by_path[0]
    raise AgentError(f"requested rendition is unavailable: {rendition}")


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        device_id: str,
        referrer: str = "",
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.user_agent = user_agent
        self.device_id = device_id
        self.referrer = referrer
        self.timeout_sec = max(1.0, timeout_sec)
        self.retries = max(1, retries)
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.request_count = 0
        self.retry_count = 0
        self.failure_count = 0
        self.elapsed_ms: list[float] = []
        self._lock = threading.Lock()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "X-Device-ID": self.device_id,
            "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            "Connection": "keep-alive",
        }
        if self.referrer:
            headers["Referer"] = self.referrer
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, float]:
        last_error = ""
        for attempt in range(1, self.retries + 1):
            started = time.perf_counter()
            try:
                request = urllib.request.Request(
                    url,
                    data=body,
                    headers=self._headers(headers),
                    method=method,
                )
                with self.opener.open(request, timeout=self.timeout_sec) as response:
                    payload = response.read()
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    with self._lock:
                        self.request_count += 1
                        self.elapsed_ms.append(elapsed_ms)
                    return response.status, payload, elapsed_ms
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                with self._lock:
                    self.request_count += 1
                    if attempt < self.retries:
                        self.retry_count += 1
                    else:
                        self.failure_count += 1
                if attempt < self.retries:
                    time.sleep(min(0.5 * attempt, 2.0))
        raise AgentError(f"HTTP request failed after {self.retries} attempts: {url}: {last_error}")

    def json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        status, response_body, _ = self.request(url, method=method, body=body, headers=headers)
        if status < 200 or status >= 300:
            raise AgentError(f"unexpected HTTP status {status}: {url}")
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"invalid JSON response from {url}: {exc}") from exc
        if not isinstance(decoded, dict):
            raise AgentError(f"expected JSON object from {url}")
        return decoded

    def text_request(self, url: str) -> str:
        status, response_body, _ = self.request(url)
        if status < 200 or status >= 300:
            raise AgentError(f"unexpected HTTP status {status}: {url}")
        return response_body.decode("utf-8", errors="replace")

    def summary(self) -> dict[str, Any]:
        return {
            "http_request_count": self.request_count,
            "http_retry_count": self.retry_count,
            "http_failure_count": self.failure_count,
            "request_elapsed_ms_mean": round(statistics.fmean(self.elapsed_ms), 3) if self.elapsed_ms else 0.0,
        }


class PlaybackRuntime:
    def __init__(self, config: ClientConfig, spec: dict[str, Any]) -> None:
        self.config = config
        self.spec = spec
        self.rng = random.Random(_safe_int(spec.get("seed"), 0))
        self.user_agent = str(spec.get("user_agent") or "Mozilla/5.0 Chrome/126.0 Safari/537.36")
        self.referrer = str(spec.get("referrer") or "")
        self.http = HttpClient(
            user_agent=self.user_agent,
            device_id=config.device_id,
            referrer=self.referrer,
            timeout_sec=_safe_float(spec.get("timeout_sec"), DEFAULT_TIMEOUT_SEC),
            retries=_safe_int(spec.get("retries"), DEFAULT_RETRIES),
        )
        self.access_token = ""
        self.session_token = ""

    def login(self) -> None:
        email = str(self.spec.get("account_email") or self.config.account_email)
        password = str(self.spec.get("account_password") or self.config.account_password)
        response = self.http.json_request(
            f"{self.config.edge_base_url.rstrip('/')}/api/auth/login",
            method="POST",
            payload={"email": email, "password": password},
        )
        self.access_token = str(response.get("accessToken") or "")
        self.session_token = str(response.get("sessionToken") or "")
        if not self.access_token or not self.session_token:
            raise AgentError("login response is missing accessToken or sessionToken")

    def browse(self, content_id: str) -> None:
        self.http.json_request(
            f"{self.config.edge_base_url.rstrip('/')}/api/browse/content/{urllib.parse.quote(content_id)}",
            access_token=self.access_token,
        )

    def start_playback(self, content_id: str, browse: bool) -> dict[str, Any]:
        if not self.access_token:
            self.login()
        if browse:
            self.browse(content_id)
        response = self.http.json_request(
            f"{self.config.edge_base_url.rstrip('/')}/api/playback/start",
            method="POST",
            payload={
                "content_id": content_id,
                "device_id": self.config.device_id,
                "session_token": self.session_token,
            },
            access_token=self.access_token,
        )
        if not response.get("manifest_url") or not response.get("token_binding"):
            raise AgentError("playback response is missing manifest_url or token_binding")
        return response

    def load_master(self, manifest_url: str) -> list[HlsVariant]:
        text = self.http.text_request(manifest_url)
        variants = parse_master_playlist(text, manifest_url)
        if not variants:
            raise AgentError(f"no variants found in master playlist: {manifest_url}")
        return variants

    def load_media(self, variant: HlsVariant) -> MediaPlaylist:
        return parse_media_playlist(self.http.text_request(variant.url), variant.url)

    def _request_segment(self, segment: HlsSegment) -> dict[str, Any]:
        try:
            status, body, elapsed_ms = self.http.request(segment.url)
            return {
                "sequence": segment.sequence,
                "status": status,
                "bytes": len(body),
                "elapsed_ms": round(elapsed_ms, 3),
                "ok": 200 <= status < 300,
            }
        except AgentError as exc:
            return {
                "sequence": segment.sequence,
                "status": 0,
                "bytes": 0,
                "elapsed_ms": 0.0,
                "ok": False,
                "error": str(exc),
            }

    def _consume_sequential(
        self,
        segments: list[HlsSegment],
        delay_min: float,
        delay_max: float,
        initial_buffer_count: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, segment in enumerate(segments):
            results.append(self._request_segment(segment))
            if index >= len(segments) - 1:
                continue
            if index + 1 < initial_buffer_count:
                time.sleep(self.rng.uniform(0.05, 0.25))
            else:
                time.sleep(self.rng.uniform(delay_min, delay_max))
        return results

    def _consume_parallel(
        self,
        segments: list[HlsSegment],
        delay_min: float,
        delay_max: float,
        parallelism: int,
    ) -> list[dict[str, Any]]:
        buckets = [segments[index::parallelism] for index in range(parallelism)]

        def worker(worker_index: int, bucket: list[HlsSegment]) -> list[dict[str, Any]]:
            worker_rng = random.Random(_safe_int(self.spec.get("seed"), 0) + 1009 * (worker_index + 1))
            worker_results: list[dict[str, Any]] = []
            for index, segment in enumerate(bucket):
                worker_results.append(self._request_segment(segment))
                if index < len(bucket) - 1:
                    time.sleep(worker_rng.uniform(delay_min, delay_max))
            return worker_results

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
            futures = [executor.submit(worker, index, bucket) for index, bucket in enumerate(buckets)]
            for future in concurrent.futures.as_completed(futures):
                results.extend(future.result())
        return sorted(results, key=lambda item: item["sequence"])

    def consume_vod(self, manifest_url: str, phases: list[dict[str, Any]]) -> dict[str, Any]:
        if not phases:
            raise AgentError("VOD consumption requires at least one phase")
        variants = self.load_master(manifest_url)
        phase_results: list[dict[str, Any]] = []
        next_index = 0
        for phase_number, phase in enumerate(phases, start=1):
            pause_before = max(0.0, _safe_float(phase.get("pause_before_sec"), 0.0))
            if pause_before:
                time.sleep(pause_before)

            rendition = str(phase.get("rendition") or "auto")
            variant = choose_variant(variants, rendition)
            playlist = self.load_media(variant)
            if not playlist.segments:
                raise AgentError(f"media playlist has no segments: {playlist.url}")

            count = max(1, _safe_int(phase.get("segment_count"), 1))
            start_mode = str(phase.get("start_mode") or "continue")
            if start_mode == "fraction":
                fraction = min(1.0, max(0.0, _safe_float(phase.get("start_fraction"), 0.0)))
                start_index = int((len(playlist.segments) - 1) * fraction)
            elif start_mode == "absolute":
                start_index = max(0, _safe_int(phase.get("start_index"), 0))
            else:
                start_index = max(0, next_index)
            selected = playlist.segments[start_index : start_index + count]
            if not selected:
                raise AgentError(
                    f"segment range is outside playlist: start={start_index}, available={len(playlist.segments)}"
                )

            delay_min = max(0.0, _safe_float(phase.get("delay_min_sec"), 5.0))
            delay_max = max(delay_min, _safe_float(phase.get("delay_max_sec"), delay_min))
            parallelism = max(1, _safe_int(phase.get("parallelism"), 1))
            if parallelism == 1:
                requests = self._consume_sequential(
                    selected,
                    delay_min,
                    delay_max,
                    max(0, _safe_int(phase.get("initial_buffer_count"), 0)),
                )
            else:
                requests = self._consume_parallel(selected, delay_min, delay_max, parallelism)

            next_index = start_index + len(selected)
            phase_results.append(
                {
                    "phase": phase_number,
                    "rendition_height": variant.height,
                    "target_duration_sec": playlist.target_duration_sec,
                    "requested_start_index": start_index,
                    "requested_segment_count": count,
                    "available_segment_count": len(playlist.segments),
                    "selected_segment_count": len(selected),
                    "first_sequence": selected[0].sequence,
                    "last_sequence": selected[-1].sequence,
                    "parallelism": parallelism,
                    "successful_segments": sum(1 for item in requests if item["ok"]),
                    "failed_segments": sum(1 for item in requests if not item["ok"]),
                    "bytes": sum(int(item["bytes"]) for item in requests),
                }
            )

        return {
            "mode": "vod",
            "variant_count": len(variants),
            "phases": phase_results,
            "successful_segments": sum(item["successful_segments"] for item in phase_results),
            "failed_segments": sum(item["failed_segments"] for item in phase_results),
        }

    def consume_live(self, manifest_url: str, live: dict[str, Any]) -> dict[str, Any]:
        variants = self.load_master(manifest_url)
        variant = choose_variant(variants, str(live.get("rendition") or "auto"))
        duration_sec = max(1.0, _safe_float(live.get("duration_sec"), 30.0))
        poll_factor = max(0.1, _safe_float(live.get("poll_factor"), 1.0))
        initial_segments = max(1, _safe_int(live.get("initial_segments"), 2))
        deadline = time.monotonic() + duration_sec
        last_requested_sequence: int | None = None
        observations: list[dict[str, Any]] = []
        successful_segments = 0
        failed_segments = 0
        first_media_sequence: int | None = None
        last_media_sequence: int | None = None

        while True:
            playlist = self.load_media(variant)
            if playlist.end_list:
                raise AgentError("LIVE playlist contains EXT-X-ENDLIST")
            if not playlist.segments:
                raise AgentError("LIVE playlist contains no segments")

            latest_sequence = playlist.segments[-1].sequence
            if first_media_sequence is None:
                first_media_sequence = playlist.media_sequence
                selected = playlist.segments[-initial_segments:]
            else:
                selected = [
                    segment
                    for segment in playlist.segments
                    if last_requested_sequence is None or segment.sequence > last_requested_sequence
                ]

            request_results = [self._request_segment(segment) for segment in selected]
            successful_segments += sum(1 for item in request_results if item["ok"])
            failed_segments += sum(1 for item in request_results if not item["ok"])
            if selected:
                last_requested_sequence = selected[-1].sequence
            last_media_sequence = playlist.media_sequence
            live_edge_lag = (
                max(0, latest_sequence - last_requested_sequence)
                if last_requested_sequence is not None
                else len(playlist.segments)
            )
            observations.append(
                {
                    "media_sequence": playlist.media_sequence,
                    "latest_sequence": latest_sequence,
                    "requested_sequences": [segment.sequence for segment in selected],
                    "live_edge_lag_segments": live_edge_lag,
                    "target_duration_sec": playlist.target_duration_sec,
                }
            )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            poll_interval = max(0.25, playlist.target_duration_sec * poll_factor)
            time.sleep(min(poll_interval, remaining))

        rolling = (
            first_media_sequence is not None
            and last_media_sequence is not None
            and last_media_sequence > first_media_sequence
        )
        return {
            "mode": "live",
            "rendition_height": variant.height,
            "duration_sec": duration_sec,
            "poll_count": len(observations),
            "successful_segments": successful_segments,
            "failed_segments": failed_segments,
            "first_media_sequence": first_media_sequence,
            "last_media_sequence": last_media_sequence,
            "rolling_playlist": rolling,
            "observations": observations,
        }

    @staticmethod
    def binding(playback: dict[str, Any]) -> dict[str, Any]:
        raw = dict(playback.get("token_binding") or {})
        required = ("token_jti", "cdn_token_id", "playback_id", "content_id", "issued_at")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise AgentError(f"playback token binding is missing: {', '.join(missing)}")
        return {key: raw[key] for key in required}

    def execute(self) -> dict[str, Any]:
        operation = str(self.spec.get("operation") or "").strip().lower()
        started = time.time()
        result: dict[str, Any]

        if operation == "issue":
            playback = self.start_playback(
                str(self.spec["content_id"]),
                bool(self.spec.get("browse", False)),
            )
            result = {
                "playbacks": [
                    {
                        "manifest_url": playback["manifest_url"],
                        "token_binding": self.binding(playback),
                    }
                ]
            }
        elif operation in {"vod", "live"}:
            playback = self.start_playback(
                str(self.spec["content_id"]),
                bool(self.spec.get("browse", False)),
            )
            if operation == "vod":
                traffic = self.consume_vod(playback["manifest_url"], list(self.spec.get("phases") or []))
            else:
                traffic = self.consume_live(playback["manifest_url"], dict(self.spec.get("live") or {}))
            result = {
                "playbacks": [
                    {
                        "manifest_url": playback["manifest_url"],
                        "token_binding": self.binding(playback),
                    }
                ],
                "traffic": traffic,
            }
        elif operation in {"consume_vod", "consume_live"}:
            manifest_url = str(self.spec.get("manifest_url") or "")
            if not manifest_url:
                raise AgentError("manifest_url is required for token consumption")
            if operation == "consume_vod":
                traffic = self.consume_vod(manifest_url, list(self.spec.get("phases") or []))
            else:
                traffic = self.consume_live(manifest_url, dict(self.spec.get("live") or {}))
            result = {"playbacks": [], "traffic": traffic}
        elif operation == "multi_vod":
            self.login()
            playbacks: list[dict[str, Any]] = []
            traffic_results: list[dict[str, Any]] = []
            flows = list(self.spec.get("flows") or [])
            if not flows:
                raise AgentError("multi_vod requires at least one flow")
            for index, flow in enumerate(flows):
                if index:
                    time.sleep(max(0.0, _safe_float(flow.get("pause_before_sec"), 0.0)))
                playback = self.start_playback(str(flow["content_id"]), bool(flow.get("browse", False)))
                traffic = self.consume_vod(playback["manifest_url"], list(flow.get("phases") or []))
                playbacks.append(
                    {
                        "manifest_url": playback["manifest_url"],
                        "token_binding": self.binding(playback),
                    }
                )
                traffic_results.append(traffic)
            result = {"playbacks": playbacks, "traffic": traffic_results}
        else:
            raise AgentError(f"unsupported operation: {operation}")

        result.update(
            {
                "ok": True,
                "source_ip": self.config.source_ip,
                "edge_id": self.config.edge_id,
                "started_epoch": started,
                "ended_epoch": time.time(),
                **self.http.summary(),
            }
        )
        traffic_items = result.get("traffic")
        if isinstance(traffic_items, dict) and traffic_items.get("failed_segments", 0):
            result["ok"] = False
        if isinstance(traffic_items, list) and any(item.get("failed_segments", 0) for item in traffic_items):
            result["ok"] = False
        return result


def print_config(config: ClientConfig, network_state: dict[str, Any]) -> int:
    public = asdict(config)
    public.pop("account_password", None)
    public["network_impairment"] = network_state
    print(json.dumps(public, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def probe(
    config: ClientConfig,
    network_state: dict[str, Any],
    timeout: float,
    retries: int,
) -> int:
    query = urllib.parse.urlencode({"probe": "logical-client"})
    url = f"{config.edge_base_url.rstrip('/')}/?{query}"
    headers = {"User-Agent": "OTT-TNSM-Probe/1.0"}
    last_error = ""
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = {
                    "logical_client_id": config.logical_client_id,
                    "configured_source_ip": config.source_ip,
                    "edge_id": config.edge_id,
                    "network_impairment": network_state,
                    "url": url,
                    "status": response.status,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
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


def idle(config: ClientConfig, network_state: dict[str, Any]) -> int:
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
                "network_impairment": network_state,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    while not stop:
        time.sleep(1)
    return 0


def decode_spec(encoded: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid run specification: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("run specification must be a JSON object")
    return decoded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show-config", help="print non-secret logical-client settings")
    subparsers.add_parser("idle", help="stay alive until the container is stopped")
    probe_parser = subparsers.add_parser("probe", help="request the assigned Edge endpoint")
    probe_parser.add_argument("--timeout", type=float, default=10.0)
    probe_parser.add_argument("--retries", type=int, default=3)
    run_parser = subparsers.add_parser("run-spec", help="execute a base64url-encoded playback specification")
    run_parser.add_argument("--spec-base64", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = ClientConfig.from_environment()
        network_state = configure_network_profile(config)
    except (AgentError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "show-config":
        return print_config(config, network_state)
    if args.command == "probe":
        return probe(
            config,
            network_state,
            timeout=args.timeout,
            retries=args.retries,
        )
    if args.command == "idle":
        return idle(config, network_state)

    try:
        result = PlaybackRuntime(config, decode_spec(args.spec_base64)).execute()
        result["network_impairment"] = network_state
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    except (AgentError, KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
