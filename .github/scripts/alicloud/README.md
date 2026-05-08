# AliCloud catalog fetcher

This UV project fetches Alibaba Cloud GPU ECS prices into a SkyPilot-style
`vms.csv`.

Required environment variables:

- `ACCESS_KEY_ID`
- `ACCESS_KEY_SECRET`

Workflow command:

```bash
uv run --project ../../../.github/scripts/alicloud python ../../../.github/scripts/alicloud/fetch_gpu_vms.py --cny-usd-rate 0.138 --max-workers 16 --output vms.csv
```
