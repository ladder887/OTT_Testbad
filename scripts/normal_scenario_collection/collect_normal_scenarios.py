#!/usr/bin/env python3
"""Normal scenario data collector for Raspberry Pi clients.

This script is a lab-side traffic generator for the OTT_Testbad platform.
It logs in with a seed account, starts playback with scenario metadata,
replays HLS manifests/segments with realistic timing, and stores watch-history
records at the end of each run.

The script is intentionally self-contained and depends only on the Python
standard library.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "profiles.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass
class RequestResult:
    status: int
    body: str
    headers: Dict[str, str]


class CollectorError(RuntimeError):
    pass


class SessionRunner:
    def __init__(self, config: Dict[str, Any], profile_name: str, session_index: int = 0):
        self.config = config
        self.profile_name = profile_name
        self.session_index = session_index
        self.defaults = config.get("defaults", {})
        self.accounts = config.get("accounts", {})
        self.profile = config.get("profiles", {}).get(profile_name)
        if not self.profile:
            raise CollectorError(f"Unknown profile: {profile_name}")

        self.api_base_url = self.profile.get("api_base_url") or self.defaults.get("api_base_url") or "http://192.168.0.100:3001"
        self.web_base_url = self.profile.get("web_base_url") or self.defaults.get("web_base_url") or "http://192.168.0.100:5173"
        self.edge_probe_urls = self.profile.get("edge_probe_urls") or self.defaults.get("edge_probe_urls") or []
        self.probe_count = int(self.profile.get("probe_count", self.defaults.get("probe_count", 5)))
        self.user_agent = self.profile.get("user_agent") or self.defaults.get("user_agent") or DEFAULT_USER_AGENT
        self.request_timeout = int(self.profile.get("request_timeout_sec", self.defaults.get("request_timeout_sec", 20)))
        self.last_selected_content_by_type: Dict[str, str] = {}
        self.random = random.Random()
        seed = f"{profile_name}:{session_index}:{time.time_ns()}"
        self.random.seed(seed)

    def run(self) -> List[Dict[str, Any]]:
        runs = self.profile.get("runs", [])
        if not runs:
            raise CollectorError(f"Profile {self.profile_name} does not contain any runs")

        run_order = StringValue(self.profile.get("run_order", "fixed")).lower()
        runnable_runs = list(runs)
        if run_order == "random":
            self.random.shuffle(runnable_runs)

        results: List[Dict[str, Any]] = []
        for run_index, run_spec in enumerate(runnable_runs, start=1):
            mode = StringValue(run_spec.get("mode", "single")).lower()
            if mode == "single":
                session_conf = run_spec.get("session") or {}
                results.append(self.run_single_session(session_conf, run_index))
                continue

            if mode == "parallel":
                session_confs = run_spec.get("sessions") or []
                results.append(self.run_parallel_sessions(session_confs, run_index))
                continue

            raise CollectorError(f"Unsupported run mode: {mode}")

        return results

    def run_single_session(self, session_conf: Dict[str, Any], run_index: int) -> Dict[str, Any]:
        repeats = int(session_conf.get("repeat", 1))
        repeat_results: List[Dict[str, Any]] = []
        for repeat_index in range(1, repeats + 1):
            repeat_results.append(self.execute_session(session_conf, run_index, repeat_index))
        return {
            "mode": "single",
            "run_index": run_index,
            "results": repeat_results,
        }

    def run_parallel_sessions(self, session_confs: Sequence[Dict[str, Any]], run_index: int) -> Dict[str, Any]:
        threads: List[threading.Thread] = []
        results: List[Optional[Dict[str, Any]]] = [None] * len(session_confs)
        errors: List[str] = []
        lock = threading.Lock()

        def worker(position: int, session_conf: Dict[str, Any]) -> None:
            try:
                results[position] = self.execute_session(session_conf, run_index, 1)
            except Exception as exc:  # pragma: no cover - surfaced to caller
                with lock:
                    errors.append(f"{session_conf.get('name', position)}: {exc}")

        for position, session_conf in enumerate(session_confs):
            thread = threading.Thread(target=worker, args=(position, session_conf), daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        if errors:
            raise CollectorError("Parallel session error(s): " + "; ".join(errors))

        return {
            "mode": "parallel",
            "run_index": run_index,
            "results": [item for item in results if item is not None],
        }

    def execute_session(self, session_conf: Dict[str, Any], run_index: int, repeat_index: int) -> Dict[str, Any]:
        session_name = StringValue(session_conf.get("name", session_conf.get("scenario_id", "session")))
        scenario_id = StringValue(session_conf.get("scenario_id", "N1")).upper()
        label = StringValue(session_conf.get("label", "normal"))
        configured_content_id = StringValue(session_conf.get("content_id", ""))
        account_key = StringValue(session_conf.get("account_key", "user1"))
        account = self.resolve_account(account_key)
        browse_queries = self._as_list(session_conf.get("browse_queries"))
        history_conf = session_conf.get("history", {})
        pattern = session_conf.get("pattern", {})

        run_id = StringValue(session_conf.get("run_id") or self._build_run_id(scenario_id, run_index, repeat_index))
        metadata = {
            "label": label,
            "run_id": run_id,
            "scenario_id": scenario_id,
            "dataset_label": StringValue(session_conf.get("dataset_label", label)),
        }

        content_selection_mode = self._content_selection_mode(session_conf)
        start_content = configured_content_id if configured_content_id else "auto"
        print(
            f"\n[{self.profile_name}] {session_name} start -> "
            f"scenario={scenario_id}, content={start_content}, mode={content_selection_mode}, account={account_key}"
        )

        access_token, session_token, user_info = self.login(account["email"], account["password"])

        content_id, selected_content = self.resolve_content_id(
            access_token,
            session_conf,
            pattern,
            metadata,
        )

        if selected_content:
            print(
                f"[{self.profile_name}] {session_name} selected -> "
                f"{content_id} ({selected_content.get('contentType', '-')})"
            )

        self._browse_phase(access_token, browse_queries, content_id, metadata)

        playback_payload = self.start_playback(access_token, content_id, metadata)
        playback_start = time.time()
        stream_params = playback_payload.get("stream_params", {})
        manifest_url = StringValue(playback_payload.get("manifest_url", ""))
        if not manifest_url:
            raise CollectorError("playback/start did not return manifest_url")

        hls_stats = self.collect_hls_traffic(manifest_url, stream_params, pattern, metadata)
        elapsed = max(1, int(time.time() - playback_start))
        watch_duration = self._int_value_from_range(
            history_conf,
            "watch_duration_sec",
            "watch_duration_min_sec",
            "watch_duration_max_sec",
            elapsed,
        )

        selected_duration_sec = self._safe_positive_int((selected_content or {}).get("durationSec"))
        total_duration_default = max(watch_duration, selected_duration_sec or 600)
        total_duration = self._int_value_from_range(
            history_conf,
            "total_duration_sec",
            "total_duration_min_sec",
            "total_duration_max_sec",
            total_duration_default,
        )
        total_duration = max(total_duration, watch_duration)
        self.save_watch_history(access_token, content_id, session_token, metadata, watch_duration, total_duration)
        self.send_edge_probes(manifest_url, metadata)

        result = {
            "profile": self.profile_name,
            "session": session_name,
            "scenario_id": scenario_id,
            "content_id": content_id,
            "content_type": (selected_content or {}).get("contentType"),
            "content_title": (selected_content or {}).get("title"),
            "content_selection_mode": content_selection_mode,
            "account": account_key,
            "run_id": run_id,
            "user_id": user_info.get("id"),
            "session_token": session_token,
            "watch_duration_sec": watch_duration,
            "total_duration_sec": total_duration,
            "requests": hls_stats,
        }
        print(f"[{self.profile_name}] {session_name} done -> {json.dumps(result, ensure_ascii=False)}")
        return result

    def resolve_account(self, account_key: str) -> Dict[str, str]:
        account = self.accounts.get(account_key)
        if not account:
            raise CollectorError(f"Unknown account key: {account_key}")
        if "email" not in account or "password" not in account:
            raise CollectorError(f"Account {account_key} must contain email/password")
        return account

    def login(self, email: str, password: str) -> Tuple[str, str, Dict[str, Any]]:
        payload = {"email": email, "password": password}
        data = self.request_json(
            "POST",
            f"{self.api_base_url}/api/auth/login",
            json_body=payload,
            headers=self.json_headers(),
        )
        access_token = StringValue(data.get("accessToken", ""))
        session_token = StringValue(data.get("sessionToken", ""))
        if not access_token or not session_token:
            raise CollectorError("login response is missing accessToken/sessionToken")
        return access_token, session_token, data.get("user", {})

    def _content_selection_mode(self, session_conf: Dict[str, Any]) -> str:
        return (
            StringValue(
                session_conf.get("content_selection")
                or self.profile.get("content_selection")
                or self.defaults.get("content_selection")
                or "fixed"
            )
            .lower()
            .strip()
        )

    def _content_type_for_session(self, session_conf: Dict[str, Any], pattern: Dict[str, Any]) -> str:
        configured_type = StringValue(
            session_conf.get("content_type")
            or pattern.get("content_type")
            or self.profile.get("content_type")
            or self.defaults.get("content_type")
        ).lower()

        if configured_type in ("vod", "live"):
            return configured_type

        return "live" if StringValue(pattern.get("type", "")).lower() == "live" else "vod"

    def fetch_content_list(
        self,
        access_token: str,
        content_type: str,
        metadata: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        query_suffix = f"?type={self._quote(content_type)}" if content_type else ""
        data = self.request_json(
            "GET",
            f"{self.api_base_url}/api/content/list{query_suffix}",
            headers=self.auth_headers(access_token, metadata),
        )

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

        if isinstance(data, dict):
            items = data.get("contents") if isinstance(data.get("contents"), list) else data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]

        return []

    def resolve_content_id(
        self,
        access_token: str,
        session_conf: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Tuple[str, Dict[str, Any]]:
        fixed_content_id = StringValue(session_conf.get("content_id", ""))
        selection_mode = self._content_selection_mode(session_conf)

        if selection_mode in ("fixed", "manual", "static") and fixed_content_id:
            return fixed_content_id, {}

        if selection_mode not in ("random", "random_from_list", "list_random", "auto"):
            if fixed_content_id:
                return fixed_content_id, {}
            selection_mode = "random_from_list"

        content_type = self._content_type_for_session(session_conf, pattern)
        items = self.fetch_content_list(access_token, content_type, metadata)

        allow_ids = {
            StringValue(item)
            for item in self._as_list(session_conf.get("content_pool"))
            if StringValue(item)
        }
        exclude_ids = {
            StringValue(item)
            for item in self._as_list(session_conf.get("exclude_content_ids"))
            if StringValue(item)
        }

        candidates: List[Dict[str, Any]] = []
        for item in items:
            item_id = StringValue(item.get("id"))
            if not item_id:
                continue
            item_type = StringValue(item.get("contentType")).lower()
            if content_type and item_type and item_type != content_type:
                continue
            if allow_ids and item_id not in allow_ids:
                continue
            if item_id in exclude_ids:
                continue
            candidates.append(item)

        if not candidates:
            if fixed_content_id:
                return fixed_content_id, {}
            raise CollectorError(
                f"No selectable contents for type={content_type}, "
                f"mode={selection_mode}, pool_size={len(items)}"
            )

        last_selected = self.last_selected_content_by_type.get(content_type)
        non_repeat_candidates = [item for item in candidates if StringValue(item.get("id")) != last_selected]
        choice_pool = non_repeat_candidates if non_repeat_candidates else candidates

        selected = self.random.choice(choice_pool)
        selected_id = StringValue(selected.get("id"))
        if not selected_id:
            raise CollectorError("Selected content item does not include id")

        self.last_selected_content_by_type[content_type] = selected_id
        return selected_id, selected

    def _browse_phase(self, access_token: str, browse_queries: Sequence[str], content_id: str, metadata: Dict[str, str]) -> None:
        if not browse_queries:
            return

        headers = self.auth_headers(access_token, metadata)
        for query in browse_queries:
            query_text = StringValue(query)
            if not query_text:
                continue
            self.request_json(
                "GET",
                f"{self.api_base_url}/api/browse/search?q={self._quote(query_text)}",
                headers=headers,
            )
            self.sleep_jitter(0.8, 2.5)

        self.request_json(
            "GET",
            f"{self.api_base_url}/api/browse/content/{self._quote(content_id)}",
            headers=headers,
        )
        self.sleep_jitter(0.5, 1.5)

    def start_playback(self, access_token: str, content_id: str, metadata: Dict[str, str]) -> Dict[str, Any]:
        payload = {
            "content_id": content_id,
            **metadata,
        }
        data = self.request_json(
            "POST",
            f"{self.api_base_url}/api/playback/start",
            json_body=payload,
            headers=self.auth_headers(access_token, metadata),
        )
        return data

    def save_watch_history(
        self,
        access_token: str,
        content_id: str,
        session_token: str,
        metadata: Dict[str, str],
        watch_duration_sec: int,
        total_duration_sec: int,
    ) -> Dict[str, Any]:
        payload = {
            "content_id": content_id,
            "session_token": session_token,
            "watch_duration": watch_duration_sec,
            "total_duration": total_duration_sec,
            **metadata,
        }
        return self.request_json(
            "POST",
            f"{self.api_base_url}/api/user/watch-history",
            json_body=payload,
            headers=self.auth_headers(access_token, metadata),
        )

    def collect_hls_traffic(
        self,
        manifest_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Dict[str, int]:
        request_counts: Counter[str] = Counter()
        signed_manifest_url = self.decorate_stream_url(manifest_url, stream_params)
        master_text = self.fetch_text(signed_manifest_url, headers=self.stream_headers(metadata))
        request_counts["manifest"] += 1

        variants = self.parse_master_variants(master_text, signed_manifest_url)
        playlist_url = self.choose_playlist_url(variants, signed_manifest_url, pattern)
        playlist_url = self.decorate_stream_url(playlist_url, stream_params)

        pattern_type = StringValue(pattern.get("type", "sequential")).lower()
        if pattern_type == "sequential":
            request_counts.update(self.play_sequential(playlist_url, stream_params, pattern, metadata))
        elif pattern_type == "seek":
            request_counts.update(self.play_seek(playlist_url, stream_params, pattern, metadata))
        elif pattern_type == "abr":
            request_counts.update(self.play_abr(variants, playlist_url, stream_params, pattern, metadata))
        elif pattern_type == "pause_resume":
            request_counts.update(self.play_pause_resume(playlist_url, stream_params, pattern, metadata))
        elif pattern_type == "live":
            request_counts.update(self.play_live(playlist_url, stream_params, pattern, metadata))
        elif pattern_type == "mixed":
            request_counts.update(self.play_mixed(playlist_url, stream_params, pattern, metadata))
        else:
            raise CollectorError(f"Unsupported playback pattern: {pattern_type}")

        return dict(request_counts)

    def play_sequential(
        self,
        playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        segment_delay = self._delay_range(pattern, 5.5, 7.0)
        segment_limit = int(pattern.get("segment_count", 30))
        playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
        counts["playlist"] += 1
        segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
        if not segment_urls:
            return counts
        for index, segment_url in enumerate(segment_urls[:segment_limit], start=1):
            self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
            counts["segment"] += 1
            if index < segment_limit:
                self.sleep_jitter(*segment_delay)
        return counts

    def play_seek(
        self,
        playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        first_count = int(pattern.get("first_segment_count", 15))
        seek_to = int(pattern.get("seek_to_segment", 60))
        after_seek_count = int(pattern.get("after_seek_count", 15))
        pause_sec = self._float_value_from_range(
            pattern,
            "seek_pause_sec",
            "seek_pause_min_sec",
            "seek_pause_max_sec",
            1.5,
        )
        segment_delay = self._delay_range(pattern, 5.5, 7.0)

        playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
        counts["playlist"] += 1
        segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
        if not segment_urls:
            return counts

        for index, segment_url in enumerate(segment_urls[:first_count], start=1):
            self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
            counts["segment"] += 1
            if index < first_count:
                self.sleep_jitter(*segment_delay)

        time.sleep(max(0.2, pause_sec))
        playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
        counts["playlist"] += 1
        segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
        if not segment_urls:
            return counts

        start_index = min(max(0, seek_to), max(0, len(segment_urls) - 1))
        for index, segment_url in enumerate(segment_urls[start_index : start_index + after_seek_count], start=1):
            self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
            counts["segment"] += 1
            if index < after_seek_count:
                self.sleep_jitter(*segment_delay)
        return counts

    def play_abr(
        self,
        variants: Sequence[Tuple[Optional[str], str]],
        fallback_playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        qualities = [StringValue(item) for item in self._as_list(pattern.get("qualities"))]
        if not qualities:
            qualities = ["1080p", "720p"]
        segments_per_quality = self._int_value_from_range(
            pattern,
            "segments_per_quality",
            "segments_per_quality_min",
            "segments_per_quality_max",
            8,
        )
        switch_delay = self._float_value_from_range(
            pattern,
            "switch_delay_sec",
            "switch_delay_min_sec",
            "switch_delay_max_sec",
            2.0,
        )
        segment_delay = self._delay_range(pattern, 5.0, 7.0)

        for quality in qualities:
            playlist_url = self.pick_variant_for_quality(variants, fallback_playlist_url, quality)
            playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
            counts["playlist"] += 1
            segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
            for index, segment_url in enumerate(segment_urls[:segments_per_quality], start=1):
                self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
                counts["segment"] += 1
                if index < segments_per_quality:
                    self.sleep_jitter(*segment_delay)
            time.sleep(max(0.2, switch_delay))
        return counts

    def play_pause_resume(
        self,
        playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        first_count = self._int_value_from_range(
            pattern,
            "first_segment_count",
            "first_segment_min_count",
            "first_segment_max_count",
            12,
        )
        second_count = self._int_value_from_range(
            pattern,
            "second_segment_count",
            "second_segment_min_count",
            "second_segment_max_count",
            20,
        )
        pause_sec = self._float_value_from_range(
            pattern,
            "pause_sec",
            "pause_min_sec",
            "pause_max_sec",
            180.0,
        )
        segment_delay = self._delay_range(pattern, 5.5, 7.0)

        playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
        counts["playlist"] += 1
        segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
        for index, segment_url in enumerate(segment_urls[:first_count], start=1):
            self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
            counts["segment"] += 1
            if index < first_count:
                self.sleep_jitter(*segment_delay)

        time.sleep(max(0.2, pause_sec))
        playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
        counts["playlist"] += 1
        segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
        for index, segment_url in enumerate(segment_urls[first_count : first_count + second_count], start=1):
            self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
            counts["segment"] += 1
            if index < second_count:
                self.sleep_jitter(*segment_delay)
        return counts

    def play_live(
        self,
        playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        poll_count = self._int_value_from_range(
            pattern,
            "poll_count",
            "poll_count_min",
            "poll_count_max",
            20,
        )
        poll_interval = self._float_value_from_range(
            pattern,
            "poll_interval_sec",
            "poll_interval_min_sec",
            "poll_interval_max_sec",
            2.5,
        )
        seen_segments: set[str] = set()

        for _ in range(poll_count):
            playlist_text = self.fetch_text(self.decorate_stream_url(playlist_url, stream_params), headers=self.stream_headers(metadata))
            counts["playlist"] += 1
            segment_urls = self.parse_media_playlist(playlist_text, playlist_url)
            for segment_url in segment_urls[-2:]:
                if segment_url in seen_segments:
                    continue
                self.fetch_text(self.decorate_stream_url(segment_url, stream_params), headers=self.stream_headers(metadata))
                seen_segments.add(segment_url)
                counts["segment"] += 1
            time.sleep(max(0.2, poll_interval))
        return counts

    def play_mixed(
        self,
        playlist_url: str,
        stream_params: Dict[str, Any],
        pattern: Dict[str, Any],
        metadata: Dict[str, str],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        phases = pattern.get("phases", [])
        if not phases:
            return counts
        for phase in phases:
            phase_type = StringValue(phase.get("type", "sequential")).lower()
            if phase_type == "sequential":
                counts.update(self.play_sequential(playlist_url, stream_params, phase, metadata))
            elif phase_type == "seek":
                counts.update(self.play_seek(playlist_url, stream_params, phase, metadata))
            elif phase_type == "abr":
                counts.update(self.play_abr([], playlist_url, stream_params, phase, metadata))
            elif phase_type == "pause_resume":
                counts.update(self.play_pause_resume(playlist_url, stream_params, phase, metadata))
            elif phase_type == "live":
                counts.update(self.play_live(playlist_url, stream_params, phase, metadata))
            else:
                raise CollectorError(f"Unsupported mixed phase: {phase_type}")
        return counts

    def send_edge_probes(self, manifest_url: str, metadata: Dict[str, str]) -> None:
        if not self.edge_probe_urls:
            return
        probe_base = self.edge_probe_urls[0] or self._base_url_from_url(manifest_url)
        probe_base = StringValue(probe_base).rstrip("/")
        headers = self.stream_headers(metadata)
        for probe_index in range(1, self.probe_count + 1):
            probe_url = f"{probe_base}/?probe={probe_index}"
            try:
                self.fetch_text(probe_url, headers=headers)
            except Exception as exc:
                print(f"[{self.profile_name}] probe failed: {probe_url}: {exc}")
                break
            self.sleep_jitter(0.1, 0.5)

    def request_json(
        self,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        payload = None if json_body is None else json.dumps(json_body).encode("utf-8")
        request_headers = headers or {}
        if json_body is not None:
            request_headers = {**request_headers, "Content-Type": "application/json"}
        request = Request(url, data=payload, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
                if not text.strip():
                    return {}
                return json.loads(text)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CollectorError(f"{method} {url} failed ({exc.code}): {body.strip() or exc.reason}") from exc
        except URLError as exc:
            raise CollectorError(f"{method} {url} failed: {exc.reason}") from exc

    def fetch_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        request = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CollectorError(f"GET {url} failed ({exc.code}): {body.strip() or exc.reason}") from exc
        except URLError as exc:
            raise CollectorError(f"GET {url} failed: {exc.reason}") from exc

    def parse_master_variants(self, text: str, base_url: str) -> List[Tuple[Optional[str], str]]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        variants: List[Tuple[Optional[str], str]] = []
        pending_resolution: Optional[str] = None
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                attributes = self.parse_attribute_line(line.split(":", 1)[1])
                pending_resolution = attributes.get("RESOLUTION")
                continue
            if line.startswith("#"):
                continue
            variants.append((pending_resolution, urljoin(base_url, line)))
            pending_resolution = None
        return variants

    def parse_media_playlist(self, text: str, base_url: str) -> List[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        segments: List[str] = []
        for line in lines:
            if line.startswith("#"):
                continue
            segments.append(urljoin(base_url, line))
        return segments

    @staticmethod
    def parse_attribute_line(value: str) -> Dict[str, str]:
        items: Dict[str, str] = {}
        current = ""
        parts: List[str] = []
        in_quotes = False
        for char in value:
            if char == '"':
                in_quotes = not in_quotes
            if char == "," and not in_quotes:
                parts.append(current)
                current = ""
                continue
            current += char
        if current:
            parts.append(current)

        for part in parts:
            if "=" not in part:
                continue
            key, raw = part.split("=", 1)
            items[key.strip()] = raw.strip().strip('"')
        return items

    def choose_playlist_url(
        self,
        variants: Sequence[Tuple[Optional[str], str]],
        fallback_url: str,
        pattern: Dict[str, Any],
    ) -> str:
        target_resolution = StringValue(pattern.get("target_resolution", "720p"))
        if not variants:
            return fallback_url
        for resolution, url in variants:
            if resolution and target_resolution in resolution:
                return url
        return variants[0][1]

    def pick_variant_for_quality(
        self,
        variants: Sequence[Tuple[Optional[str], str]],
        fallback_url: str,
        quality: str,
    ) -> str:
        for resolution, url in variants:
            if resolution and quality in resolution:
                return url
        return fallback_url

    def decorate_stream_url(self, url: str, stream_params: Dict[str, Any]) -> str:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query_map = {
            "token": stream_params.get("token"),
            "sig": stream_params.get("sig"),
            "content_id": stream_params.get("content_id"),
            "user_id": stream_params.get("user_id"),
            "user": stream_params.get("username"),
            "sid": stream_params.get("session_id"),
            "label": stream_params.get("label"),
            "run_id": stream_params.get("run_id"),
            "scenario_id": stream_params.get("scenario_id"),
            "dataset_label": stream_params.get("dataset_label"),
        }
        for key, value in query_map.items():
            if value not in (None, "") and key not in query:
                query[key] = StringValue(value)
        return urlunsplit(parsed._replace(query=urlencode(query)))

    def auth_headers(self, access_token: str, metadata: Dict[str, str]) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            **self.base_headers(metadata),
        }

    def stream_headers(self, metadata: Dict[str, str]) -> Dict[str, str]:
        return self.base_headers(metadata)

    def base_headers(self, metadata: Dict[str, str]) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "X-Scenario-Label": StringValue(metadata.get("label", "normal")),
            "X-Run-ID": StringValue(metadata.get("run_id", "")),
            "X-Scenario-ID": StringValue(metadata.get("scenario_id", "")),
            "X-Dataset-Label": StringValue(metadata.get("dataset_label", metadata.get("label", "normal"))),
        }

    def json_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def sleep_jitter(self, minimum: float, maximum: float) -> None:
        if maximum <= 0:
            return
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        time.sleep(self.random.uniform(max(0.0, minimum), max(0.0, maximum)))

    def _delay_range(self, pattern: Dict[str, Any], default_min: float, default_max: float) -> Tuple[float, float]:
        minimum = float(pattern.get("min_delay_sec", default_min))
        maximum = float(pattern.get("max_delay_sec", default_max))
        return minimum, maximum

    def _int_value_from_range(
        self,
        source: Dict[str, Any],
        fixed_key: str,
        min_key: str,
        max_key: str,
        fallback: int,
    ) -> int:
        if source.get(min_key) is not None or source.get(max_key) is not None:
            minimum = int(source.get(min_key, fallback))
            maximum = int(source.get(max_key, source.get(min_key, fallback)))
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            return self.random.randint(max(1, minimum), max(1, maximum))
        return int(source.get(fixed_key, fallback))

    def _float_value_from_range(
        self,
        source: Dict[str, Any],
        fixed_key: str,
        min_key: str,
        max_key: str,
        fallback: float,
    ) -> float:
        if source.get(min_key) is not None or source.get(max_key) is not None:
            minimum = float(source.get(min_key, fallback))
            maximum = float(source.get(max_key, source.get(min_key, fallback)))
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            return self.random.uniform(max(0.0, minimum), max(0.0, maximum))
        return float(source.get(fixed_key, fallback))

    @staticmethod
    def _safe_positive_int(value: Any) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _as_list(self, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _build_run_id(self, scenario_id: str, run_index: int, repeat_index: int) -> str:
        today = dt.datetime.now().strftime("%Y%m%d")
        return f"{today}_{self.profile_name}_{scenario_id.lower()}_{run_index:02d}_{repeat_index:02d}"

    def _quote(self, value: str) -> str:
        return urlencode({"q": value}).split("=", 1)[1]

    def _base_url_from_url(self, url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}"


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise CollectorError(f"Config file not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def list_profiles(config: Dict[str, Any]) -> List[str]:
    profiles = config.get("profiles", {})
    return sorted(profiles.keys())


def StringValue(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect normal-scenario OTT traffic for Raspberry Pi clients")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to profiles.json")
    parser.add_argument("--profile", default="all", help="Profile name to run, or 'all'")
    parser.add_argument("--list-profiles", action="store_true", help="Print available profile names and exit")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.list_profiles:
        for profile_name in list_profiles(config):
            print(profile_name)
        return 0

    profile_names = list_profiles(config) if args.profile == "all" else [args.profile]
    if not profile_names:
        raise CollectorError("No profiles available in configuration")

    overall_results: List[Dict[str, Any]] = []
    for profile_name in profile_names:
        runner = SessionRunner(config, profile_name)
        results = runner.run()
        overall_results.append({"profile": profile_name, "results": results})

    print("\n=== Summary ===")
    print(json.dumps(overall_results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CollectorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
