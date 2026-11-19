# data-integration-iceberg-snapshot-restore on Huawei Cloud

[![License: MIT No Attribution](https://img.shields.io/badge/License-MIT%20No%20Attribution-blue.svg)](LICENSE)
![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-verified-green.svg)
![Build](https://img.shields.io/badge/build-passing-brightgreen.svg)

## Overview

Apache Iceberg is an open table format that supports ACID transactions, schema evolution, and time travel, and is commonly used as the table storage format for data lakes. In the Huawei Cloud data lake house stack (Data Lake Insight / Map Reduce Service / DataArts Studio), Iceberg table metadata files are stored on Object Storage Service (OBS), and each table has a metadata pointer that references the latest metadata file.

When engineers upgrade the infrastructure that hosts Iceberg tables, the metadata pointer may be reset: during the upgrade the metadata is rewritten to a new (empty) version, and if the pointer is not proactively restored, downstream query engines read empty metadata and the table appears to have lost its data. In reality the data files and historical metadata are always safely stored; restoring the pointer to its pre-upgrade position recovers all historical snapshots.

This practice provides a **three-phase, no-data-loss workflow**: capture → upgrade → restore, covering the typical scenario of Iceberg table infrastructure upgrades.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Highlights](#highlights)
- [Cloud Services](#cloud-services)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Step-by-Step Deployment](#step-by-step-deployment)
- [Usage](#usage)
- [Cleanup](#cleanup)
- [Details](#details)
- [Dependencies](#dependencies)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)

## Architecture

```
                  ┌────────────────────────────────────────────┐
                  │   FunctionGraph (Python 3.10)               │
                  │                                            │
                  │   pre_migration   post_migration   rollback │
                  │   (capture)       (restore)       (rollback)│
                  └─────────────┬──────────────────────────────┘
                                │ IAM agency (least privilege)
                                ▼
              ┌──────────────────────────────────────────────┐
              │      Object Storage Service OBS (2 buckets)    │
              │  ┌─────────────────┐  ┌─────────────────────┐ │
              │  │ <metadata-bucket>│  │  <data-bucket>      │ │
              │  │   snapshots/     │  │   <db>/             │ │
              │  │   manifests/     │  │     metadata/       │ │
              │  └─────────────────┘  │     data/           │ │
              │                       └─────────────────────┘ │
              └──────────────────────────────────────────────┘
                                │
                                ▼
                  ┌────────────────────────────────────────────┐
                  │   Log Tank Service LTS (log group + streams)│
                  └────────────────────────────────────────────┘
```

## Highlights

- **Serverless architecture**: functions are triggered on demand, with no long-running compute required.
- **Two physically isolated OBS buckets**: the metadata bucket and the data bucket are separated to prevent accidental operations from affecting real Iceberg data.
- **Least-privilege IAM agency**: each function maps to one IAM agency whose permissions are strictly scoped to buckets and prefixes.
- **trace_id call-chain tracing**: every function passes a `trace_id` through logs and responses; operators query LTS by `trace_id` to correlate all logs of one invocation.
- **Idempotent design**: restore and rollback operations can be repeated without side effects.

## Cloud Services

This practice uses the following Huawei Cloud services:

| Service | Purpose |
|---|---|
| Object Storage Service (OBS) | Stores metadata snapshots, manifest indexes, and Iceberg table data |
| FunctionGraph | Hosts the capture / restore / rollback functions |
| Identity and Access Management (IAM) | Provides least-privilege access agencies for the functions |
| Log Tank Service (LTS) | Collects function logs and supports querying by `trace_id` |

> After use, release the resources following the Cleanup section to avoid leftover environments.

## Prerequisites

- A Huawei Cloud account with access keys: obtain the Access Key and Secret Key from the "My Credentials" page in the Huawei Cloud console and configure them in your local environment.
- Terraform `>= 1.5.0`.
- Python `>= 3.7` (used to package function code locally).
- huaweicloud Provider `~> 1.97` and null Provider `~> 3.2`.
- Region: this practice uses `cn-north-4` as an example; adjust via the `region` variable.

## Quick Start

```bash
# 1. Enter the infrastructure directory
cd data-integration-iceberg-snapshot-restore/infra

# 2. Prepare the variables file (set the two OBS bucket names)
cp terraform.tfvars.example terraform.tfvars

# 3. Package function code locally (generates infra/.build/*.zip)
python3 scripts/build_zips.py ../src/functions .build

# 4. Initialize and deploy
terraform init
terraform apply
```

## Step-by-Step Deployment

### Step 1: Prepare variables

Copy the example variables file and fill in two globally unique OBS bucket names:

```bash
cd data-integration-iceberg-snapshot-restore/infra
cp terraform.tfvars.example terraform.tfvars
```

Key fields in `terraform.tfvars`:

| Field | Description |
|---|---|
| `metadata_bucket_name` | Metadata bucket name, created by this practice |
| `data_bucket_name` | Data bucket name, created by this practice (holds Iceberg table data) |
| `name_prefix` | Resource name prefix |
| `region` | Huawei Cloud region |
| `output_prefix` | Snapshot file key prefix |
| `log_retention_days` | LTS log retention days |

### Step 2: Package functions

```bash
python3 scripts/build_zips.py ../src/functions .build
```

Expected output:

```
[+] Built <path>/.build/pre_migration.zip
[+] Built <path>/.build/post_migration.zip
[+] Built <path>/.build/rollback_metadata.zip
```

> The script also installs `esdk-obs-python` into the build directory and packages it into the zip, so the function is self-contained at runtime.

### Step 3: Initialize and deploy

```bash
terraform init
terraform apply
```

After `terraform apply` completes, it outputs the three functions' URN / names and the OBS prefixes for snapshots and manifests.

## Usage

All three functions are triggered via FunctionGraph with a JSON event.

### Phase 1 · Capture (before upgrade)

```json
{
  "databases": ["database_a", "database_b"]
}
```

Invoke `pre_migration` to capture the metadata pointer of every Iceberg table under each database, write a snapshot to the metadata bucket, and return the snapshot's OBS URI.

### Phase 2 · Upgrade

The infrastructure upgrade is performed by an upgrade flow outside this practice. Iceberg table data files and historical metadata always remain in the data bucket.

### Phase 3 · Restore (after upgrade)

```json
{
  "database": "database_a",
  "input_key": "iceberg-metadata/database_a/20260917T103045Z.json"
}
```

Invoke `post_migration` to restore each table's pointer from the snapshot into the manifest, returning the `restored` / `unchanged` / `skipped` / `failed` status per table.

### Phase 4 · Emergency rollback

```json
{
  "database": "database_a"
}
```

Invoke `rollback` to reset each table's pointer to the 4th-latest metadata file based on the timestamp order in the data bucket, returning the `rolled_back` / `unchanged` / `skipped` status per table.

> **Verification note**: after deployment, run the three functions on your local PC following this section to confirm the capture, restore, and rollback flows work as expected before considering the delivery complete.

## Cleanup

```bash
cd data-integration-iceberg-snapshot-restore/infra
terraform destroy
```

This releases all resources created by this practice (functions, IAM agencies, LTS logs, OBS buckets) and avoids leftover environments.

## Details

- Architecture design, workflow details, the trace_id mechanism, and limitations are in [docs/architecture.md](docs/architecture.md).
- Deployment parameters, permission boundaries, and troubleshooting are in [docs/deployment-guide.md](docs/deployment-guide.md).

## Dependencies

| Dependency | Version | Description |
|---|---|---|
| Terraform | `>= 1.5.0` | Infrastructure orchestration |
| huaweicloud Provider | `~> 1.97` | Huawei Cloud resource orchestration |
| null Provider | `~> 3.2` | Local function packaging |
| Python | `>= 3.7` | Function runtime and packaging script |
| esdk-obs-python | latest | SDK for OBS access (auto-installed at packaging time) |

## FAQ

**Q: The function reports `Missing credentials` at runtime?**
A: Confirm the function is attached to its IAM agency (created by Terraform) and the agency grants the required OBS permissions. In agency mode, temporary credentials are injected automatically at runtime.

**Q: `terraform plan` reports the `.build/*.zip` file is missing?**
A: Run the packaging command in "Step 2" of the deployment section to generate the zip files before `terraform init` / `apply`.

**Q: Can the rollback step size be adjusted?**
A: Modify the `ROLLBACK_OFFSET` constant (default 3, i.e. the 4th-latest version) in `src/functions/rollback_metadata.py`, then repackage and redeploy.

## Contributing

Issues and Pull Requests are welcome. Before submitting, confirm that local `terraform validate` passes and the function code has been repackaged. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

This practice is licensed under MIT No Attribution. See [LICENSE](LICENSE).

## Contact

- Channel: report issues and suggestions through repository Issues.

## 17. Creator and Maintainer

| Role | GitHub Account |
|---|---|
| **Creator** | `dgc-git` |
| **Maintainer** | `dgc-git` |
