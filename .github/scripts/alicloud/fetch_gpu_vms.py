#!/usr/bin/env python3
"""Fetch Alibaba Cloud GPU VM prices into a SkyPilot-style vms.csv."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from alibabacloud_credentials.client import Client as CredentialClient
from alibabacloud_ecs20140526.client import Client as ECSClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


DEFAULT_ENDPOINT_REGION = "cn-hangzhou"
DEFAULT_OUTPUT = Path(__file__).with_name("vms.csv")
DEFAULT_ENV_FILE = Path(__file__).with_name(".env")
CSV_FIELDS = [
    "InstanceType",
    "AcceleratorName",
    "AcceleratorCount",
    "vCPUs",
    "MemoryGiB",
    "GpuInfo",
    "Price",
    "SpotPrice",
    "Region",
    "AvailabilityZone",
    "Generation",
    "Arch",
]


@dataclass(frozen=True)
class InstanceSpec:
    instance_type: str
    accelerator_name: str
    accelerator_count: int
    vcpus: int
    memory_gib: float
    gpu_info: str
    generation: str
    arch: str


@dataclass(frozen=True)
class PriceTask:
    region_id: str
    zone_id: str
    spec: InstanceSpec


@dataclass
class Summary:
    regions: int = 0
    zones: int = 0
    tasks: int = 0
    rows: int = 0
    skipped_prices: int = 0
    failures: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Alibaba Cloud GPU ECS prices and write a dashboard-compatible vms.csv.",
    )
    parser.add_argument(
        "--cny-usd-rate",
        type=positive_float,
        required=True,
        help="Required exchange rate multiplier from CNY to USD, for example 0.138.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"CSV output path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--regions",
        help="Optional comma-separated region allowlist, for example cn-hangzhou,cn-shanghai.",
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=4,
        help="Maximum concurrent price requests. Defaults to 4.",
    )
    parser.add_argument(
        "--spot-duration",
        type=non_negative_int,
        default=0,
        help="Spot duration passed to DescribePrice. Defaults to 0.",
    )
    return parser.parse_args()


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def create_client(region_id: str = DEFAULT_ENDPOINT_REGION) -> ECSClient:
    access_key_id = (
        os.environ.get("ACCESS_KEY_ID")
        or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
        or os.environ.get("accessKeyId")
    )
    access_key_secret = (
        os.environ.get("ACCESS_KEY_SECRET")
        or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        or os.environ.get("accessKeySecret")
    )
    if access_key_id and access_key_secret:
        config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
    else:
        credential = CredentialClient()
        config = open_api_models.Config(credential=credential)
    config.endpoint = f"ecs.{region_id}.aliyuncs.com"
    config.region_id = region_id
    return ECSClient(config)


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def create_runtime() -> util_models.RuntimeOptions:
    return util_models.RuntimeOptions(
        autoretry=True,
        max_attempts=2,
        connect_timeout=10000,
        read_timeout=10000,
    )


def get_sequence(value: object, attr: str) -> list:
    current = getattr(value, attr, None)
    if current is None:
        return []
    if isinstance(current, list):
        return current
    return list(current)


def get_trade_price(response: object) -> float | None:
    price_info = getattr(getattr(response, "body", None), "price_info", None)
    price = getattr(price_info, "price", None)
    trade_price = getattr(price, "trade_price", None)
    if trade_price is None:
        return None
    try:
        parsed = float(trade_price)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def format_number(value: float | int | None) -> str:
    if value is None:
        return ""
    parsed = float(value)
    if not parsed:
        return ""
    return f"{parsed:.8f}".rstrip("0").rstrip(".")


def normalize_gpu_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("NVIDIA ", "").replace("GPU", "").strip()


def fetch_regions(client: ECSClient, runtime: util_models.RuntimeOptions) -> list[str]:
    request = ecs_models.DescribeRegionsRequest(resource_type="instance")
    response = client.describe_regions_with_options(request, runtime)
    regions = get_sequence(getattr(response.body, "regions", None), "region")
    return sorted(region.region_id for region in regions if getattr(region, "region_id", None))


def fetch_gpu_specs(client: ECSClient, runtime: util_models.RuntimeOptions) -> dict[str, InstanceSpec]:
    specs: dict[str, InstanceSpec] = {}
    next_token: str | None = None

    while True:
        request = ecs_models.DescribeInstanceTypesRequest(
            minimum_gpuamount=1,
            max_results=100,
            next_token=next_token,
        )
        response = client.describe_instance_types_with_options(request, runtime)
        instance_types = get_sequence(getattr(response.body, "instance_types", None), "instance_type")

        for item in instance_types:
            instance_type = getattr(item, "instance_type_id", "") or ""
            gpu_count = int(getattr(item, "gpuamount", 0) or 0)
            gpu_name = normalize_gpu_name(getattr(item, "gpuspec", ""))
            if not instance_type or gpu_count <= 0 or not gpu_name:
                continue

            gpu_memory = getattr(item, "gpumemory_size", None)
            gpu_info = f"{gpu_name} {format_number(gpu_memory)}GB".strip() if gpu_memory else gpu_name
            specs[instance_type] = InstanceSpec(
                instance_type=instance_type,
                accelerator_name=gpu_name,
                accelerator_count=gpu_count,
                vcpus=int(getattr(item, "cpu_core_count", 0) or 0),
                memory_gib=float(getattr(item, "memory_size", 0) or 0),
                gpu_info=gpu_info,
                generation=getattr(item, "instance_type_family", "") or "",
                arch=getattr(item, "cpu_architecture", "") or "",
            )

        next_token = getattr(response.body, "next_token", None)
        if not next_token:
            break

    return specs


def fetch_zone_instance_types(
    client: ECSClient,
    runtime: util_models.RuntimeOptions,
    region_id: str,
) -> dict[str, set[str]]:
    request = ecs_models.DescribeZonesRequest(
        region_id=region_id,
        instance_charge_type="PostPaid",
        verbose=True,
    )
    response = client.describe_zones_with_options(request, runtime)
    zones = get_sequence(getattr(response.body, "zones", None), "zone")
    zone_types: dict[str, set[str]] = {}

    for zone in zones:
        zone_id = getattr(zone, "zone_id", None)
        if not zone_id:
            continue
        instance_types: set[str] = set()
        available_instance_types = getattr(zone, "available_instance_types", None)
        instance_types.update(get_sequence(available_instance_types, "instance_types"))

        available_resources = getattr(zone, "available_resources", None)
        resources = get_sequence(available_resources, "resources_info")
        for resource in resources:
            resource_instance_types = getattr(resource, "instance_types", None)
            instance_types.update(get_sequence(resource_instance_types, "supported_instance_type"))

        if instance_types:
            zone_types[zone_id] = instance_types

    return zone_types


def build_price_tasks(
    regions: Iterable[str],
    specs: dict[str, InstanceSpec],
    runtime: util_models.RuntimeOptions,
    summary: Summary,
) -> list[PriceTask]:
    tasks: list[PriceTask] = []
    for region_id in regions:
        client = create_client(region_id)
        try:
            zone_types = fetch_zone_instance_types(client, runtime, region_id)
        except Exception as error:  # noqa: BLE001 - SDK errors vary by transport/runtime.
            summary.failures += 1
            print(f"warning: failed to list zones for {region_id}: {error}", file=sys.stderr)
            continue

        summary.zones += len(zone_types)
        for zone_id, instance_types in zone_types.items():
            for instance_type in sorted(instance_types):
                spec = specs.get(instance_type)
                if spec:
                    tasks.append(PriceTask(region_id=region_id, zone_id=zone_id, spec=spec))

    summary.tasks = len(tasks)
    return tasks


def fetch_one_price(
    client: ECSClient,
    region_id: str,
    zone_id: str,
    instance_type: str,
    *,
    spot: bool,
    spot_duration: int,
    runtime: util_models.RuntimeOptions,
) -> float | None:
    request_kwargs = {
        "region_id": region_id,
        "zone_id": zone_id,
        "instance_type": instance_type,
        "resource_type": "instance",
        "instance_network_type": "vpc",
        "price_unit": "Hour",
        "amount": 1,
        "system_disk": ecs_models.DescribePriceRequestSystemDisk(
            category="cloud_essd",
            size=40,
        ),
    }
    if spot:
        request_kwargs.update(
            {
                "spot_duration": spot_duration,
                "spot_strategy": "SpotAsPriceGo",
            },
        )

    request = ecs_models.DescribePriceRequest(**request_kwargs)
    response = client.describe_price_with_options(request, runtime)
    return get_trade_price(response)


def fetch_price_row(
    task: PriceTask,
    cny_usd_rate: float,
    spot_duration: int,
    runtime: util_models.RuntimeOptions,
) -> tuple[dict[str, str] | None, int, int]:
    client = create_client(task.region_id)
    skipped_prices = 0
    failures = 0

    try:
        price_cny = fetch_one_price(
            client,
            task.region_id,
            task.zone_id,
            task.spec.instance_type,
            spot=False,
            spot_duration=spot_duration,
            runtime=runtime,
        )
    except Exception:  # noqa: BLE001 - individual price misses should not stop the fetch.
        price_cny = None
        failures += 1

    try:
        spot_cny = fetch_one_price(
            client,
            task.region_id,
            task.zone_id,
            task.spec.instance_type,
            spot=True,
            spot_duration=spot_duration,
            runtime=runtime,
        )
    except Exception:  # noqa: BLE001
        spot_cny = None
        failures += 1

    if price_cny is None:
        skipped_prices += 1
        return None, skipped_prices, failures

    row = {
        "InstanceType": task.spec.instance_type,
        "AcceleratorName": task.spec.accelerator_name,
        "AcceleratorCount": str(task.spec.accelerator_count),
        "vCPUs": str(task.spec.vcpus),
        "MemoryGiB": format_number(task.spec.memory_gib),
        "GpuInfo": task.spec.gpu_info,
        "Price": format_number(price_cny * cny_usd_rate),
        "SpotPrice": format_number(spot_cny * cny_usd_rate) if spot_cny is not None else "",
        "Region": task.region_id,
        "AvailabilityZone": task.zone_id,
        "Generation": task.spec.generation,
        "Arch": task.spec.arch,
    }
    return row, skipped_prices, failures


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    load_env_file()
    started_at = time.monotonic()
    runtime = create_runtime()
    summary = Summary()

    discovery_client = create_client()
    specs = fetch_gpu_specs(discovery_client, runtime)
    if not specs:
        print("error: no GPU instance types found from DescribeInstanceTypes.", file=sys.stderr)
        return 1

    if args.regions:
        regions = sorted({region.strip() for region in args.regions.split(",") if region.strip()})
    else:
        regions = fetch_regions(discovery_client, runtime)
    summary.regions = len(regions)

    tasks = build_price_tasks(regions, specs, runtime, summary)
    rows: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = [
            executor.submit(
                fetch_price_row,
                task,
                args.cny_usd_rate,
                args.spot_duration,
                runtime,
            )
            for task in tasks
        ]
        for future in as_completed(futures):
            row, skipped_prices, failures = future.result()
            summary.skipped_prices += skipped_prices
            summary.failures += failures
            if row is not None:
                rows.append(row)

    rows.sort(key=lambda row: (row["Region"], row["AvailabilityZone"], row["InstanceType"]))
    write_csv(args.output, rows)
    summary.rows = len(rows)
    elapsed = time.monotonic() - started_at
    print(
        "summary: "
        f"regions={summary.regions} zones={summary.zones} tasks={summary.tasks} "
        f"rows={summary.rows} skipped_prices={summary.skipped_prices} "
        f"failures={summary.failures} output={args.output} elapsed={elapsed:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
