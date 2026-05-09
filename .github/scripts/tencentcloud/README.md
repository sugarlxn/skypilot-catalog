# TencentCloud catalog fetcher

This UV project fetches TencentCloud GPU CVM prices into a SkyPilot-style
`vms.csv`.

The GitHub workflow writes the generated catalog to:

```text
catalogs/v8/tencentcloud/vms.csv
```

## Required GitHub Actions secrets

Configure these repository secrets before running
`.github/workflows/fetch-tencentcloud-catalog.yaml`:

- `TENCENTCLOUD_SECRET_ID`
- `TENCENTCLOUD_SECRET_KEY`
- `TENCENTCLOUD_IMAGE_ID`

`TENCENTCLOUD_IMAGE_ID` must be a valid public image ID usable by
`InquiryPriceRunInstances`; for example, `img-r8qb077f` was tested with
TencentOS Server 4 AI.

The TencentCloud CAM user must be allowed to call:

- `cvm:DescribeRegions`
- `cvm:DescribeInstanceTypeConfigs`
- `cvm:InquiryPriceRunInstances`

`QcloudCVMFullAccess` is sufficient for the current fetcher.

## Workflow command

The workflow installs dependencies with:

```bash
uv sync --project .github/scripts/tencentcloud --locked
```

It then runs the fetcher from `catalogs/v8/tencentcloud`:

```bash
uv run --project ../../../.github/scripts/tencentcloud python ../../../.github/scripts/tencentcloud/fetch_tencentcloud_catalog.py --cny-usd-rate 0.138 --workers 8 --retries 3 --retry-backoff 0.5 --output vms.csv
```

The workflow is triggered manually by `workflow_dispatch` and automatically
after `fetch-alicloud-catalog` succeeds.

## Local smoke test

From the repository root, after exporting the required environment variables:

```bash
uv sync --project .github/scripts/tencentcloud --locked
uv run --project .github/scripts/tencentcloud python .github/scripts/tencentcloud/fetch_tencentcloud_catalog.py --region ap-guangzhou --image-id "$TENCENTCLOUD_IMAGE_ID" --cny-usd-rate 0.138 --output /tmp/tencentcloud-vms.csv
```

Useful options:

- `--workers 8` controls concurrent price requests per region.
- `--retries 3` retries transient price request failures.
- `--retry-backoff 0.5` sets the initial retry backoff in seconds.
- `--region REGION` can be repeated to limit a local test.
