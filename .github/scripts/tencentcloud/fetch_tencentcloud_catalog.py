#!/usr/bin/env python3
"""Fetch TencentCloud GPU VM prices into a SkyPilot-compatible CSV."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.cvm.v20170312 import cvm_client, models


DEFAULT_CNY_USD_RATE = 0.138
DEFAULT_WORKERS = 8
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "vms.csv"

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

# TencentCloud's public GPU CVM docs map instance families to accelerator
# models. Families listed as only "NVIDIA GPU" are intentionally omitted so
# their rows are skipped until the exact model is known.
GPU_FAMILY_TO_ACCELERATOR = {
    "GI1": "Intel SG1",
    "GI3X": "T4",
    "GN6": "P4",
    "GN6S": "P4",
    "GN7": "T4",
    "GN7VI": "T4",
    "GN7VW": "T4",
    "GN8": "P40",
    "GN10X": "V100",
    "GN10XP": "V100",
    "GNV4": "A10",
    "GNV4V": "A10",
    "GT4": "A100",
    "PNV4": "A10",
    "PTX1": "Tencent Zixiao C100",
}


@dataclass
class Stats:
    regions_seen: int = 0
    regions_failed: int = 0
    gpu_specs_seen: int = 0
    skipped_unknown_gpu: int = 0
    price_failed: int = 0
    rows_written: int = 0


@dataclass(frozen=True)
class PriceJob:
    region: str
    zone: str
    instance_type: str
    instance_family: str
    accelerator_name: str
    accelerator_count: Any
    vcpus: Any
    memory_gib: Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch TencentCloud GPU CVM catalog data into vms.csv."
    )
    parser.add_argument(
        "--image-id",
        default=os.getenv("TENCENTCLOUD_IMAGE_ID"),
        help="TencentCloud ImageId used by InquiryPriceRunInstances. "
        "Defaults to TENCENTCLOUD_IMAGE_ID.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--cny-usd-rate",
        type=float,
        default=None,
        help="CNY to USD conversion rate. Defaults to CNY_USD_RATE or a "
        f"built-in {DEFAULT_CNY_USD_RATE}.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0,
        help="Seconds each worker sleeps after a price request. Defaults to 0.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("TENCENTCLOUD_WORKERS", DEFAULT_WORKERS)),
        help="Concurrent price request workers per region. Defaults to "
        f"TENCENTCLOUD_WORKERS or {DEFAULT_WORKERS}.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries for each price request after transient failures. Defaults to 3.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.5,
        help="Initial retry backoff seconds, doubled each retry. Defaults to 0.5.",
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit collection to one TencentCloud region. Can be repeated.",
    )
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def get_cny_usd_rate(cli_rate: float | None) -> float:
    if cli_rate is not None:
        return cli_rate

    env_rate = os.getenv("CNY_USD_RATE")
    if env_rate:
        try:
            return float(env_rate)
        except ValueError as exc:
            raise SystemExit(f"Invalid CNY_USD_RATE value: {env_rate}") from exc

    print(
        f"warning: CNY_USD_RATE not set; using built-in {DEFAULT_CNY_USD_RATE}",
        file=sys.stderr,
    )
    return DEFAULT_CNY_USD_RATE


def build_client(cred: credential.Credential, region: str = "") -> cvm_client.CvmClient:
    http_profile = HttpProfile()
    http_profile.endpoint = "cvm.tencentcloudapi.com"

    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    return cvm_client.CvmClient(cred, region, client_profile)


def api_json(response: Any) -> dict[str, Any]:
    return json.loads(response.to_json_string())


def get_available_regions(client: cvm_client.CvmClient) -> list[str]:
    req = models.DescribeRegionsRequest()
    req.from_json_string("{}")
    data = api_json(client.DescribeRegions(req))
    return [
        item["Region"]
        for item in data.get("RegionSet", [])
        if item.get("Region") and item.get("RegionState") == "AVAILABLE"
    ]


def describe_instance_types(client: cvm_client.CvmClient) -> list[dict[str, Any]]:
    req = models.DescribeInstanceTypeConfigsRequest()
    req.from_json_string("{}")
    data = api_json(client.DescribeInstanceTypeConfigs(req))
    return data.get("InstanceTypeConfigSet", [])


def inquiry_hourly_price_cny(
    client: cvm_client.CvmClient,
    *,
    zone: str,
    image_id: str,
    instance_type: str,
) -> float | None:
    req = models.InquiryPriceRunInstancesRequest()
    params = {
        "Placement": {"Zone": zone},
        "ImageId": image_id,
        "InstanceChargeType": "POSTPAID_BY_HOUR",
        "InstanceType": instance_type,
        "InstanceCount": 1,
    }
    req.from_json_string(json.dumps(params))
    data = api_json(client.InquiryPriceRunInstances(req))
    item_price = data.get("Price", {}).get("InstancePrice", {})

    for key in ("UnitPriceDiscount", "UnitPrice"):
        value = item_price.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def field(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def is_gpu_spec(row: dict[str, Any]) -> bool:
    gpu = field(row, "GPU", "Gpu", default=0) or 0
    gpu_count = field(row, "GpuCount", "GPUCount", default=0) or 0
    return float(gpu) > 0 or float(gpu_count) > 0


def normalize_family(value: Any) -> str:
    return str(value or "").replace("-", "").upper()


def accelerator_for_family(instance_family: Any) -> str | None:
    return GPU_FAMILY_TO_ACCELERATOR.get(normalize_family(instance_family))


def format_number(value: float | int | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def format_price(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def price_job(
    cred: credential.Credential,
    job: PriceJob,
    *,
    image_id: str,
    cny_usd_rate: float,
    sleep_seconds: float,
    retries: int,
    retry_backoff: float,
) -> dict[str, str]:
    price_cny = None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            client = build_client(cred, job.region)
            price_cny = inquiry_hourly_price_cny(
                client,
                zone=job.zone,
                image_id=image_id,
                instance_type=job.instance_type,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            break
        except TencentCloudSDKException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(retry_backoff * (2**attempt))

    if last_error is not None and price_cny is None:
        raise last_error
    if price_cny is None:
        raise ValueError(
            f"missing hourly price for {job.region}/{job.zone}/{job.instance_type}"
        )

    price_usd = price_cny * cny_usd_rate
    return {
        "InstanceType": job.instance_type,
        "AcceleratorName": job.accelerator_name,
        "AcceleratorCount": format_number(job.accelerator_count),
        "vCPUs": format_number(job.vcpus),
        "MemoryGiB": format_number(job.memory_gib),
        "GpuInfo": (
            f"{job.accelerator_name} x{format_number(job.accelerator_count)}, "
            f"family {job.instance_family}"
        ),
        "Price": format_price(price_usd),
        "SpotPrice": "",
        "Region": job.region,
        "AvailabilityZone": job.zone,
        "Generation": job.instance_family,
        "Arch": "",
    }


def collect_rows(
    cred: credential.Credential,
    regions: list[str],
    *,
    image_id: str,
    cny_usd_rate: float,
    sleep_seconds: float,
    workers: int,
    retries: int,
    retry_backoff: float,
) -> tuple[list[dict[str, str]], Stats]:
    rows: list[dict[str, str]] = []
    stats = Stats(regions_seen=len(regions))

    for region in regions:
        client = build_client(cred, region)
        print(f"collecting region {region}", file=sys.stderr)
        try:
            configs = describe_instance_types(client)
        except TencentCloudSDKException as exc:
            stats.regions_failed += 1
            print(f"warning: failed to describe {region}: {exc}", file=sys.stderr)
            continue

        price_jobs: list[PriceJob] = []
        for config in configs:
            if not is_gpu_spec(config):
                continue

            stats.gpu_specs_seen += 1
            instance_family = field(config, "InstanceFamily", default="")
            accelerator_name = accelerator_for_family(instance_family)
            if not accelerator_name:
                stats.skipped_unknown_gpu += 1
                continue

            instance_type = field(config, "InstanceType", default="")
            zone = field(config, "Zone", default="")
            if not instance_type or not zone:
                stats.price_failed += 1
                print(
                    f"warning: missing instance type or zone in {region}: {config}",
                    file=sys.stderr,
                )
                continue

            accelerator_count = field(config, "GpuCount", "GPU", "Gpu", default=0)
            price_jobs.append(
                PriceJob(
                    region=region,
                    zone=str(zone),
                    instance_type=str(instance_type),
                    instance_family=str(instance_family),
                    accelerator_name=accelerator_name,
                    accelerator_count=accelerator_count,
                    vcpus=field(config, "CPU", "Cpu", default=0),
                    memory_gib=field(config, "Memory", "MemoryGiB", default=0),
                )
            )

        if not price_jobs:
            continue

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    price_job,
                    cred,
                    job,
                    image_id=image_id,
                    cny_usd_rate=cny_usd_rate,
                    sleep_seconds=sleep_seconds,
                    retries=retries,
                    retry_backoff=retry_backoff,
                ): job
                for job in price_jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    rows.append(future.result())
                    stats.rows_written += 1
                except (TencentCloudSDKException, ValueError) as exc:
                    stats.price_failed += 1
                    print(
                        "warning: failed to price "
                        f"{job.region}/{job.zone}/{job.instance_type}: {exc}",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    stats.price_failed += 1
                    print(
                        "warning: failed to price "
                        f"{job.region}/{job.zone}/{job.instance_type}: {exc}",
                        file=sys.stderr,
                    )

    rows.sort(
        key=lambda item: (
            item["Region"],
            item["AvailabilityZone"],
            item["AcceleratorName"],
            float(item["AcceleratorCount"] or 0),
            item["InstanceType"],
        )
    )
    return rows, stats


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if not args.image_id:
        raise SystemExit(
            "Missing image id: pass --image-id or set TENCENTCLOUD_IMAGE_ID. "
            "TencentCloud requires ImageId for InquiryPriceRunInstances."
        )
    if args.workers < 1:
        raise SystemExit("--workers must be greater than 0")
    if args.retries < 0:
        raise SystemExit("--retries cannot be negative")
    if args.retry_backoff < 0:
        raise SystemExit("--retry-backoff cannot be negative")

    secret_id = require_env("TENCENTCLOUD_SECRET_ID")
    secret_key = require_env("TENCENTCLOUD_SECRET_KEY")
    cny_usd_rate = get_cny_usd_rate(args.cny_usd_rate)
    cred = credential.Credential(secret_id, secret_key)

    if args.regions:
        regions = args.regions
    else:
        regions = get_available_regions(build_client(cred))

    rows, stats = collect_rows(
        cred,
        regions,
        image_id=args.image_id,
        cny_usd_rate=cny_usd_rate,
        sleep_seconds=args.sleep,
        workers=args.workers,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
    )
    write_csv(args.output, rows)

    print(
        "done: "
        f"regions={stats.regions_seen}, "
        f"region_failures={stats.regions_failed}, "
        f"gpu_specs={stats.gpu_specs_seen}, "
        f"unknown_gpu_skipped={stats.skipped_unknown_gpu}, "
        f"price_failures={stats.price_failed}, "
        f"rows={stats.rows_written}, "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
