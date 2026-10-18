# AWS guide: every piece, in simple English

This explains each AWS resource in `infra/` **before** it is created: what it is, why
this project needs it, how it is locked down, and what it costs. Prices are on-demand
us-east-1 list prices; check them against the AWS Pricing Calculator before applying.

## The big picture

Think of the AWS account as an empty building you rent by the hour.

- The **VPC** is your private floor in that building, with its own address range.
- **Subnets** are rooms on that floor. A *public* room has a door to the street (a route to
  the internet gateway); a *private* room has no door to the street at all.
- **Security groups** are bouncers standing at each machine. They check every connection
  against a short list ("only the API host may talk to the database on port 5432").
- **IAM roles** are badges. A machine wears a badge that lets it open exactly the doors
  it needs, and nothing else. The badge's keys rotate automatically every few hours.

## Network (`network.tf`)

| Resource | What and why |
|---|---|
| VPC `10.20.0.0/16` | Our private network. DNS hostnames are on so names like the RDS endpoint resolve inside it. |
| Internet gateway | The door to the street. Only the public subnet's route table points at it. |
| Public subnet (1 AZ) | Holds the EC2 host. It gets a public IP so it can make *outgoing* HTTPS calls (pull images, talk to SSM, Secrets Manager, CloudWatch). Nothing can come *in*: see the security group. |
| 2 private subnets (2 AZs) | Hold RDS and the Lambda. Their route table has no internet route at all. RDS requires subnets in two availability zones even for a single-AZ database. |
| Default security group, emptied | Every VPC has a default group that allows all traffic between its members. We strip its rules so nothing can use it by accident. |

**Why no NAT gateway?** A NAT gateway lets private machines reach the internet. It costs
about $0.045/hour (~$33/month) plus $0.045 per GB, the biggest line on many small AWS
bills. Here only the EC2 host needs the internet, and it sits in the public subnet with
no inbound access, so a NAT gateway buys nothing.

## Security groups (`security_groups.tf`)

| Group | Inbound | Outbound |
|---|---|---|
| EC2 | **none** | 443 to anywhere (AWS APIs, OS packages); 5432 to the RDS group |
| Lambda | none | 5432 to the RDS group |
| RDS | 5432 from the EC2 group and the Lambda group | none needed (security groups are stateful: replies are allowed automatically) |

Rules reference *groups*, not IP addresses, so they stay correct if IPs change.

## EC2 host (`ec2.tf`, `user_data.sh.tftpl`)

- **t3.micro, Amazon Linux 2023**, with Docker installed at first boot, plus 1 GiB swap
  (the machine has only 1 GiB RAM).
- Runs two containers: the API (gunicorn) and the fake GPU-metrics service.
- **No SSH**: no key pair, no port 22. Admin access and deploys use **SSM** (Systems
  Manager). The SSM agent on the host keeps an outbound HTTPS connection to AWS, and
  commands come back over it. "Session Manager" gives a shell; "Run Command" runs a
  script; "port forwarding" tunnels a local port to the host.
- The API listens on **127.0.0.1:8080 only**, so even a security-group mistake would
  not expose it. You reach it through an SSM port-forward tunnel.
- **IMDSv2 only, hop limit 1**: the instance metadata service (where the host's temporary
  AWS credentials live) needs a session token, and containers cannot reach it. This blocks
  a common way stolen credentials leak (SSRF).
- **CPU credits "standard"**: T3 defaults to "unlimited", which bills extra if the CPU
  stays busy. Standard mode just slows down instead.
- **Instance role** (least privilege): pull images from *this* ECR repository, read
  *these two* secrets, write to *this* log group, plus the AWS-managed SSM policy.

## RDS PostgreSQL 16 (`rds.tf`)

- `db.t4g.micro`, 20 GB gp3, **single-AZ** (a standby in a second AZ would double the cost).
- **Private**: `publicly_accessible = false`, private subnets, and only two security
  groups may connect.
- **Encrypted at rest** with the AWS-managed key (free), and **TLS required**
  (`rds.force_ssl = 1`). The API connects with `sslmode=verify-full`, checking the
  server's certificate against Amazon's RDS CA bundle baked into the image.
- **Admin password managed by RDS** (`manage_master_user_password`): RDS generates it and
  stores it in Secrets Manager. Terraform never sees it.
- **Three database roles**: the admin (migrations only), `triage_app` (the API: SELECT,
  INSERT, UPDATE; no DELETE, no schema changes), and `dq_reader` (the Lambda: SELECT
  only, logs in with IAM tokens).
- Practice-only settings so `terraform destroy` is clean: no deletion protection, no
  final snapshot, 1 day of backups. In production you would flip all three.

## ECR and Secrets Manager (`ecr_secrets.tf`)

- **ECR** is a private Docker registry. Tags are **immutable** and equal to the git SHA,
  so "version abc123" always means the same bytes and rollback means "run the previous
  SHA". Scan-on-push checks OS packages for known vulnerabilities. A lifecycle rule keeps
  the 10 newest images.
- **Secrets Manager** holds the API's database password and the ingest API key
  (generated by Terraform, never typed by a person). The EC2 role may read them; the
  GitHub deploy role may not. `recovery_window_in_days = 0` deletes them at once on
  destroy (practice only; the default 7-30 day window protects against mistakes).

## The data-quality Lambda (`lambda.tf`)

A Python function that runs once a day (EventBridge Scheduler, 07:00 New York time) and
checks the data: freshness, volume, duplicates, missing fields, unknown share and
reconciliation against what each collector run said it found.

**The problem:** it must sit inside the VPC to reach the private database, and a Lambda
in a VPC has **no internet**, so it cannot call AWS APIs like Secrets Manager or SNS.

| Option | How it works | Cost | Verdict |
|---|---|---|---|
| NAT gateway | gives the private subnets internet access | ~$33/month + $0.045/GB | too expensive for one daily function |
| VPC interface endpoints (Secrets Manager, SNS, CloudWatch) | private doors to each AWS service inside the VPC | ~$0.01/hour per endpoint per AZ: ~$7.30/month each in one AZ, ~$14.60 in two; three services ≈ $22-44/month | works, but costs more than the rest of the stack |
| **RDS IAM auth + logs-based metrics (chosen)** | the Lambda signs a 15-minute login token *locally* with its role (no network call), and reports by printing JSON; Lambda ships its own logs to CloudWatch from outside the VPC, and an Embedded Metric Format line becomes the `FailedChecks` metric that an alarm watches | $0 | chosen |

Trade-offs of the chosen option: IAM auth needs TLS and is meant for modest connection
rates (fine for one connection a day); the alarm fires from the metric, so the email says
"FailedChecks >= 1" and you read the details in the Lambda's log. With a NAT gateway
or endpoints, the function could send a detailed report itself.

Two separate alarms on purpose: **Lambda errors** = the checker itself broke;
**FailedChecks** = the checker ran and found bad data.

## CloudWatch and SNS (`monitoring.tf`)

- The API writes one **JSON log line per request** to stdout. Docker's `awslogs` driver
  sends it to the log group `/rc-triage/api` (kept 7 days).
- **Metric filters** turn log lines into metrics without any agent: `RequestCount`,
  `Http5xxCount` (lines with `status >= 500`) and `LatencyMs` (the `duration_ms` field).
- **Alarms**, all emailing through one **SNS topic**:
  - **5xx rate** above 5 % for 2 minutes (a *rate*, ignoring minutes with fewer than 5
    requests: 3 errors out of 3 is an outage, 3 out of 3,000 is noise);
  - **p95 latency** above 1,000 ms for 3 minutes (p95 = 95 % of requests were faster);
  - **Lambda errors** ≥ 1;
  - **Failed data-quality checks** ≥ 1 (stays in ALARM until a later run is clean).
- SNS email needs you to click a confirmation link once.

## GitHub OIDC (`github_oidc.tf`)

GitHub Actions can prove who it is with a short-lived **OpenID Connect token** ("I am repo
X, running for environment production"). AWS trusts GitHub's identity provider and
trades that token for **1-hour credentials** of the deploy role, but only if the token's
`sub` is exactly `repo:<owner>/<repo>:environment:production`. So:

- there are **no AWS keys stored in GitHub** to leak or rotate;
- a fork, another repository, or a job that skipped the approval gate gets nothing;
- the role can only push to one ECR repository and send commands to one instance. It
  cannot read secrets, touch the database, or change infrastructure.

## Rough cost while it runs

| Item | Price | Per hour |
|---|---|---|
| EC2 t3.micro | $0.0104/hour | $0.0104 |
| EC2 public IPv4 address | $0.005/hour | $0.0050 |
| EBS gp3 10 GB | $0.08/GB-month | $0.0011 |
| RDS db.t4g.micro PostgreSQL, single-AZ | $0.016/hour | $0.0160 |
| RDS gp3 20 GB | $0.115/GB-month | $0.0032 |
| Secrets Manager, 2 secrets | $0.40/secret-month | $0.0011 |
| CloudWatch: 5 custom metrics, 5 alarm metrics, a few MB of logs | free tier covers 10 metrics, 10 alarms, 5 GB logs | ~$0 (≤ $0.003 without free tier) |
| ECR (< 1 GB), Lambda (1 run/day), Scheduler, SNS email, KMS (AWS-managed keys) | free tier or fractions of a cent | ~$0 |
| **Total** | | **≈ $0.04/hour ≈ $1/day ≈ $29/month if left running** |

Not created on purpose: NAT gateway (~$0.045/hour), VPC endpoints (~$0.01/hour each per
AZ), load balancer (~$0.0225/hour + capacity units), Multi-AZ RDS (2x RDS cost).
Plan: run it for a day or two, verify, record, then `terraform destroy`.
