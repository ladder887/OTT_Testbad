"""Audit completed experiment manifests before a main collection starts."""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = REPOSITORY_ROOT / "03_experiments" / "07_generated" / "logical_clients.json"
EXPECTED_SCENARIOS = {*(f"N{index}" for index in range(1, 8)), "A1", "A2", "A3", "A6", "A7"}
EXPECTED_VARIANTS = {
    "N1": {"preview", "standard", "long", "catalog_preview"},
    "N2": {"default"},
    "N3": {"default"},
    "N4": {"default"},
    "N5": {"default"},
    "N6": {"household", "flash_crowd"},
    "N7": {"single", "popular_channel"},
    "A1": {"low_fanout", "high_fanout"},
    "A2": {"fast", "stealth"},
    "A3": {"low_parallel", "high_parallel"},
    "A6": {"low_rate"},
    "A7": {"low_fanout", "high_fanout"},
}
REQUIRED_MAIN_VARIANTS = {
    (scenario_id, variant)
    for scenario_id, variants in EXPECTED_VARIANTS.items()
    for variant in variants
}
REQUIRED_HARD_NEGATIVE_VARIANTS = {
    ("N1", "catalog_preview"),
    ("A2", "stealth"),
    ("N6", "flash_crowd"),
    ("A6", "low_rate"),
    ("N7", "popular_channel"),
    ("A7", "low_fanout"),
}
EXPECTED_PROFILES = {f"P{index}" for index in range(5)}
EXPECTED_PROFILE_VALUES = {
    "P0": (0.0, 0.0),
    "P1": (18.0, 0.0),
    "P2": (45.0, 0.2998),
    "P3": (90.0, 0.9975),
    "P4": (170.0, 0.4994),
}


class AuditError(RuntimeError):
    """Raised when audit inputs cannot be loaded."""


def expand_manifest_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        candidate = Path(value)
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.json")))
            continue
        matches = [Path(item) for item in glob.glob(value)]
        if matches:
            paths.extend(sorted(matches))
        elif candidate.exists():
            paths.append(candidate)
    resolved = sorted(
        {
            path.resolve()
            for path in paths
            if not path.name.endswith(
                (".validation.json", ".execution.json", ".campaign_state.json")
            )
        }
    )
    if not resolved:
        raise AuditError("no run manifests matched")
    return resolved


def load_inventory(path: Path) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("clients", [])
    inventory = {str(item["logical_client_id"]): item for item in rows}
    if len(inventory) != 100:
        raise AuditError(f"expected 100 inventory clients, found {len(inventory)}")
    return inventory


def validation_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".validation.json")


def selected_clients(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in manifest.get("parameters", {}).get("selected_clients", [])
        if isinstance(item, dict)
    ]


def result_by_client(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("logical_client_id") or ""): item
        for item in manifest.get("parameters", {}).get("client_results", [])
        if isinstance(item, dict) and item.get("logical_client_id")
    }


def scenario_errors(manifest: dict[str, Any]) -> list[str]:
    scenario_id = str(manifest.get("scenario_id") or "")
    parameters = manifest.get("parameters", {})
    bindings = manifest.get("token_bindings", [])
    variant = str(parameters.get("scenario_variant") or "")
    errors: list[str] = []

    if variant and variant not in EXPECTED_VARIANTS.get(scenario_id, set()):
        errors.append(f"{scenario_id} has unsupported scenario_variant: {variant}")

    if scenario_id == "N1" and variant == "catalog_preview":
        content_count = int(parameters.get("content_count") or 0)
        if content_count < 2 or len(bindings) != content_count:
            errors.append("N1 catalog_preview requires at least two contents with separate tokens")
        if len({str(item.get("content_id") or "") for item in bindings} - {""}) != content_count:
            errors.append("N1 catalog_preview requires a different content for every preview token")
        if int(parameters.get("concurrency") or 0) != 1:
            errors.append("N1 catalog_preview must remain serial")

    if scenario_id in {"A1", "A7"}:
        if parameters.get("shared_token") is not True or len(bindings) != 1:
            errors.append(f"{scenario_id} must use one shared token")
        if int(parameters.get("consumer_count") or 0) < 2:
            errors.append(f"{scenario_id} must have at least two consumers")
        if variant == "low_fanout" and int(parameters.get("consumer_count") or 0) != 2:
            errors.append(f"{scenario_id} low_fanout must use exactly two consumers")
        if variant == "high_fanout" and int(parameters.get("consumer_count") or 0) < 3:
            errors.append(f"{scenario_id} high_fanout must use at least three consumers")
    elif scenario_id == "A2":
        if int(parameters.get("content_count") or 0) < 2:
            errors.append("A2 must harvest at least two contents")
        if int(parameters.get("concurrency") or 0) != 1:
            errors.append("A2 must remain serial")
        if variant and parameters.get("download_variant") != variant:
            errors.append("A2 scenario_variant and download_variant do not match")
    elif scenario_id == "A3":
        if int(parameters.get("worker_count") or 0) < 2:
            errors.append("A3 must use at least two workers")
        if variant == "low_parallel" and int(parameters.get("worker_count") or 0) != 2:
            errors.append("A3 low_parallel must use exactly two workers")
        if variant == "high_parallel" and int(parameters.get("worker_count") or 0) < 3:
            errors.append("A3 high_parallel must use at least three workers")
    elif scenario_id == "A6":
        if int(parameters.get("participant_count") or 0) != 4:
            errors.append("A6 must use four participant containers")
        if int(parameters.get("actual_account_count") or 0) < 2:
            errors.append("A6 must use at least two accounts")
        if int(parameters.get("actual_token_count") or 0) < 2 or len(bindings) < 2:
            errors.append("A6 must use at least two real tokens")
    elif scenario_id == "N6":
        if int(parameters.get("consumer_count") or 0) < 2 or len(bindings) < 2:
            errors.append("N6 must use at least two consumers with separate tokens")
        if parameters.get("shared_token") is not False:
            errors.append("N6 must not share one CDN token across viewers")
        if variant == "household":
            if parameters.get("shared_account") is not True or int(parameters.get("actual_account_count") or 0) != 1:
                errors.append("N6 household must use one shared account with separate tokens")
        elif variant == "flash_crowd":
            content_ids = {str(item.get("content_id") or "") for item in bindings}
            if parameters.get("shared_account") is not False:
                errors.append("N6 flash_crowd must use independent accounts")
            if parameters.get("shared_content") is not True or len(content_ids - {""}) != 1:
                errors.append("N6 flash_crowd must watch one shared content")
            if int(parameters.get("actual_account_count") or 0) < 2:
                errors.append("N6 flash_crowd requires at least two independent accounts")

    if scenario_id == "N7" and variant == "popular_channel":
        consumer_count = int(parameters.get("consumer_count") or 0)
        content_ids = {str(item.get("content_id") or "") for item in bindings}
        if consumer_count < 2 or len(bindings) != consumer_count:
            errors.append("N7 popular_channel requires at least two viewers with separate tokens")
        if parameters.get("shared_token") is not False:
            errors.append("N7 popular_channel must use independent CDN tokens")
        if len(content_ids - {""}) != 1:
            errors.append("N7 popular_channel viewers must watch the same LIVE content")

    if scenario_id in {"N7", "A7"}:
        results = parameters.get("client_results", [])
        if not results or any(
            not item.get("traffic", {}).get("rolling_playlist")
            for item in results
            if isinstance(item, dict) and item.get("role") != "token_owner"
        ):
            errors.append(f"{scenario_id} requires a rolling LIVE playlist for every viewer")
    return errors


def audit(
    manifest_paths: list[Path],
    inventory: dict[str, dict[str, str]],
    mode: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    run_ids: set[str] = set()
    token_ids: set[str] = set()
    scenario_counts: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()
    coverage: dict[str, Counter[str]] = {
        "physical_host_id": Counter(),
        "edge_id": Counter(),
        "network_profile_id": Counter(),
        "content_id": Counter(),
    }
    class_coverage: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: {key: Counter() for key in coverage}
    )
    applied_profiles: Counter[str] = Counter()
    validation_passed = 0
    retry_count = 0
    failure_count = 0
    superseded_failed_manifest_count = 0
    dataset_prefixes: Counter[str] = Counter()
    run_reports: list[dict[str, Any]] = []

    completed_matrix_run_keys: Counter[str] = Counter()
    for path in manifest_paths:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        matrix_run_key = str(candidate.get("parameters", {}).get("matrix_run_key") or "")
        if candidate.get("status") == "completed" and matrix_run_key:
            completed_matrix_run_keys[matrix_run_key] += 1
    duplicate_completed_keys = sorted(
        key for key, count in completed_matrix_run_keys.items() if count > 1
    )
    if duplicate_completed_keys:
        errors.append(f"matrix run keys have multiple completed manifests: {duplicate_completed_keys}")

    for path in manifest_paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: cannot read manifest: {exc}")
            continue
        run_id = str(manifest.get("run_id") or "")
        scenario_id = str(manifest.get("scenario_id") or "")
        class_name = "normal" if scenario_id.startswith("N") else "attack"
        status = str(manifest.get("status") or "")
        matrix_run_key = str(manifest.get("parameters", {}).get("matrix_run_key") or "")
        run_errors: list[str] = []

        if status != "completed" and matrix_run_key and completed_matrix_run_keys[matrix_run_key] == 1:
            superseded_failed_manifest_count += 1
            warnings.append(
                f"{path.name}: superseded failed attempt for matrix run key {matrix_run_key}"
            )
            run_reports.append(
                {
                    "manifest": str(path),
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "scenario_variant": str(manifest.get("parameters", {}).get("scenario_variant") or ""),
                    "matrix_run_key": matrix_run_key,
                    "status": "superseded_failed",
                    "validation_passed": False,
                    "selected_client_count": len(selected_clients(manifest)),
                    "errors": [],
                }
            )
            continue

        if not run_id or run_id in run_ids:
            run_errors.append(f"duplicate or missing run_id: {run_id or '<missing>'}")
        run_ids.add(run_id)
        if status != "completed":
            run_errors.append(f"status is {status or '<missing>'}, not completed")
        if scenario_id not in EXPECTED_SCENARIOS:
            run_errors.append(f"unsupported scenario_id: {scenario_id or '<missing>'}")
        else:
            scenario_counts[scenario_id] += 1
            variant = str(manifest.get("parameters", {}).get("scenario_variant") or "")
            if variant:
                variant_counts[f"{scenario_id}:{variant}"] += 1
        dataset_prefixes[str(manifest.get("dataset_prefix") or "")] += 1

        validation_file = validation_path(path)
        validation_pass = False
        if not validation_file.exists():
            run_errors.append("collection validation report is missing")
        else:
            try:
                validation = json.loads(validation_file.read_text(encoding="utf-8"))
                validation_pass = validation.get("passed") is True
            except (OSError, json.JSONDecodeError) as exc:
                run_errors.append(f"cannot read validation report: {exc}")
            if not validation_pass:
                run_errors.append("collection validation did not pass")
        validation_passed += int(validation_pass)

        clients = selected_clients(manifest)
        results = result_by_client(manifest)
        if not clients:
            run_errors.append("selected_clients is empty")
        for client in clients:
            logical_client_id = str(client.get("logical_client_id") or "")
            canonical = inventory.get(logical_client_id)
            if canonical is None:
                run_errors.append(f"unknown logical client: {logical_client_id or '<missing>'}")
                continue
            for field in ("physical_host_id", "source_ip", "edge_id", "network_profile_id"):
                actual = str(client.get(field) or "")
                expected = str(canonical.get(field) or "")
                if actual != expected:
                    run_errors.append(
                        f"{logical_client_id}: {field}={actual or '<missing>'}, expected {expected}"
                    )
            for field in ("physical_host_id", "edge_id", "network_profile_id"):
                value = str(client.get(field) or "")
                coverage[field][value] += 1
                class_coverage[class_name][field][value] += 1

            result = results.get(logical_client_id)
            if result is None:
                run_errors.append(f"{logical_client_id}: client result is missing")
                continue
            retry_count += int(result.get("http_retry_count") or 0)
            failure_count += int(result.get("http_failure_count") or 0)
            network = result.get("network_impairment")
            if not isinstance(network, dict):
                if mode == "main":
                    run_errors.append(f"{logical_client_id}: network impairment result is missing")
                continue
            assigned_profile = str(client.get("network_profile_id") or "")
            applied_profile = str(network.get("profile_id") or "")
            if applied_profile != assigned_profile:
                run_errors.append(
                    f"{logical_client_id}: applied profile {applied_profile or '<missing>'} "
                    f"does not match {assigned_profile}"
                )
                continue
            if assigned_profile not in EXPECTED_PROFILE_VALUES:
                run_errors.append(f"{logical_client_id}: unknown network profile {assigned_profile}")
                continue
            expected_rtt, expected_loss = EXPECTED_PROFILE_VALUES[assigned_profile]
            actual_rtt = float(network.get("configured_added_rtt_ms") or 0.0)
            actual_loss = float(network.get("approximate_end_to_end_loss_percent") or 0.0)
            if abs(actual_rtt - expected_rtt) > 0.01 or abs(actual_loss - expected_loss) > 0.01:
                run_errors.append(
                    f"{logical_client_id}: applied values do not match {assigned_profile}"
                )
            else:
                applied_profiles[assigned_profile] += 1

        for binding in manifest.get("token_bindings", []):
            token_id = str(binding.get("cdn_token_id") or "")
            if not token_id or token_id in token_ids:
                run_errors.append(f"duplicate or missing token binding: {token_id or '<missing>'}")
            token_ids.add(token_id)
            content_id = str(binding.get("content_id") or "")
            if content_id:
                coverage["content_id"][content_id] += 1
                class_coverage[class_name]["content_id"][content_id] += 1

        run_errors.extend(scenario_errors(manifest))
        errors.extend(f"{path.name}: {message}" for message in run_errors)
        run_reports.append(
            {
                "manifest": str(path),
                "run_id": run_id,
                "scenario_id": scenario_id,
                "scenario_variant": str(manifest.get("parameters", {}).get("scenario_variant") or ""),
                "validation_passed": validation_pass,
                "selected_client_count": len(clients),
                "errors": run_errors,
            }
        )

    missing_scenarios = sorted(EXPECTED_SCENARIOS - set(scenario_counts))
    missing_main_variants = sorted(
        f"{scenario_id}:{variant}"
        for scenario_id, variant in REQUIRED_MAIN_VARIANTS
        if variant_counts[f"{scenario_id}:{variant}"] == 0
    )
    missing_hard_negative_variants = sorted(
        f"{scenario_id}:{variant}"
        for scenario_id, variant in REQUIRED_HARD_NEGATIVE_VARIANTS
        if variant_counts[f"{scenario_id}:{variant}"] == 0
    )
    missing_applied_profiles = sorted(EXPECTED_PROFILES - set(applied_profiles))
    if mode in {"scenario", "main"} and missing_scenarios:
        errors.append(f"required scenarios are missing: {missing_scenarios}")
    if mode == "main" and missing_main_variants:
        errors.append(f"required scenario variants are missing: {missing_main_variants}")
    if mode == "hard-negative" and missing_hard_negative_variants:
        errors.append(
            f"required hard-negative pilot variants are missing: {missing_hard_negative_variants}"
        )
    if mode in {"network", "main"}:
        if missing_applied_profiles:
            errors.append(f"network profiles were not actually applied: {missing_applied_profiles}")
        if len(coverage["edge_id"]) < 4:
            errors.append("network pilot must cover all four Edges")
        if len(coverage["physical_host_id"]) < 5:
            errors.append("network pilot must cover at least five physical hosts")

    for class_name in ("normal", "attack"):
        profiles = set(class_coverage[class_name]["network_profile_id"])
        missing = sorted(EXPECTED_PROFILES - profiles)
        if missing:
            warnings.append(f"{class_name} class has no samples assigned to profiles: {missing}")
        edges = set(class_coverage[class_name]["edge_id"])
        if len(edges) < 4:
            warnings.append(f"{class_name} class covers only {len(edges)} of four Edges")

    if len(dataset_prefixes) > 1:
        warnings.append(f"multiple dataset prefixes were audited: {sorted(dataset_prefixes)}")
    if failure_count:
        warnings.append(f"client HTTP failures observed: {failure_count}")

    return {
        "passed": not errors,
        "mode": mode,
        "manifest_count": len(manifest_paths),
        "validation_passed_count": validation_passed,
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "variant_counts": dict(sorted(variant_counts.items())),
        "missing_scenarios": missing_scenarios,
        "missing_main_variants": missing_main_variants,
        "missing_hard_negative_variants": missing_hard_negative_variants,
        "coverage": {
            key: dict(sorted(counter.items()))
            for key, counter in coverage.items()
        },
        "class_coverage": {
            class_name: {
                key: dict(sorted(counter.items()))
                for key, counter in fields.items()
            }
            for class_name, fields in sorted(class_coverage.items())
        },
        "applied_network_profiles": dict(sorted(applied_profiles.items())),
        "missing_applied_network_profiles": missing_applied_profiles,
        "http_retry_count": retry_count,
        "http_failure_count": failure_count,
        "superseded_failed_manifest_count": superseded_failed_manifest_count,
        "dataset_prefixes": dict(sorted(dataset_prefixes.items())),
        "errors": errors,
        "warnings": warnings,
        "runs": run_reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", nargs="+", required=True)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--mode",
        choices=("scenario", "hard-negative", "network", "main"),
        default="scenario",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        paths = expand_manifest_paths(args.manifests)
        report = audit(paths, load_inventory(args.inventory.resolve()), args.mode)
    except (AuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"pilot audit failed: {exc}", file=sys.stderr)
        return 2
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["output_path"] = str(output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
