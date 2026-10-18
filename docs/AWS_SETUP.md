# AWS setup: the steps only you can do

Do these **before** `terraform apply`. Never paste keys, passwords or tokens into a chat,
a commit, or a file in this repo.

## 1. Make the account safe (once)

1. **Root user MFA.** Sign in as the root user (the email you signed up with), open
   *IAM → Dashboard → Add MFA*, and add an authenticator app or a passkey. The root user
   can close the account and change billing, so it must never be usable with just a password.
2. **Stop using root.** Create a daily identity with admin rights and MFA:
   *IAM Identity Center* (recommended) or an *IAM user* with the `AdministratorAccess`
   policy and MFA. Do not create access keys for root, ever.
3. **Budget alarm.** *Billing and Cost Management → Budgets → Create budget*:
   - use the **Zero spend** template (emails you when spending goes above $0.01), **and**
   - a **monthly cost budget of $10**, with alerts at 50 % and 100 % of *actual* cost and
     100 % of *forecasted* cost, sent to your email.
   Also turn on *Billing preferences → Free Tier usage alerts*.

## 2. Log in from the terminal

AWS CLI 2.32+ can sign in with your console login and gives short-lived credentials:

```bash
aws login                                   # opens the browser; pick your admin identity
aws sts get-caller-identity                 # the Arn must NOT end in ":root"
```

(Identity Center users can use `aws configure sso` instead.)

## 3. Install the Session Manager plugin

Needed to reach the API through SSM port forwarding (no open ports). Install the
Windows plugin from the AWS documentation page
"Install the Session Manager plugin for the AWS CLI", then check:

```bash
session-manager-plugin --version
```

## 4. Fill in the Terraform variables

```bash
cp infra/example.tfvars infra/terraform.tfvars   # git-ignored
# edit: alert_email, github_repo
```

## 5. Plan, read, apply

```bash
python scripts/build_lambda.py
cd infra && terraform init && terraform plan -out tfplan
```

Read the plan: about 45 resources, nothing deleted. Then `terraform apply tfplan`.
RDS takes 5 to 10 minutes. Afterwards, **click the confirmation link** in the email from
AWS Notifications, or the alarms cannot reach you.

## 6. First deploy

The pipeline deploys on every merge to `main` (see section 7). For the very first image, or
without GitHub, deploy from the laptop:

```bash
REPO_URL=$(terraform -chdir=infra output -raw ecr_repository_url)
INSTANCE=$(terraform -chdir=infra output -raw instance_id)
TAG=$(git rev-parse HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URL%%/*}"
docker build --build-arg APP_VERSION="$TAG" -t "$REPO_URL:$TAG" . && docker push "$REPO_URL:$TAG"
PYTHON=python bash deploy/send_deploy.sh "$INSTANCE" "$TAG"
```

Then open a tunnel and use the API:

```bash
$(terraform -chdir=infra output -raw port_forward_command)    # leave it running
curl http://127.0.0.1:8080/health                              # in a second terminal
```

The ingest API key lives in Secrets Manager. Load it into your shell without printing it:

```bash
export TRIAGE_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id rc-triage/app --query SecretString --output text \
  | python -c "import json,sys; print(json.load(sys.stdin)['ingest_api_key'])")
```

## 7. GitHub (CD)

- Repository variables: `DEPLOY_ENABLED=true`, `AWS_REGION=us-east-1`.
- Environment `production` with **required reviewer** (you), and environment variables
  `AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY_URL`, `EC2_INSTANCE_ID` from `terraform output`.
- No AWS keys anywhere in GitHub: the workflow gets 1-hour credentials through OIDC.

## After destroy

```bash
cd infra && terraform destroy
# then: GitHub variable DEPLOY_ENABLED=false
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=rc-triage-aws \
  --query 'ResourceTagMappingList[].ResourceARN'
aws ec2 describe-instances --filters Name=tag:Project,Values=rc-triage-aws \
  Name=instance-state-name,Values=pending,running,stopping,stopped --query 'Reservations[].Instances[].InstanceId'
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'
aws ec2 describe-addresses --query 'Addresses[].PublicIp'
aws ec2 describe-nat-gateways --filter Name=state,Values=available,pending --query 'NatGateways[].NatGatewayId'
aws ec2 describe-network-interfaces --filters Name=description,Values="AWS Lambda VPC ENI*" --query 'NetworkInterfaces[].NetworkInterfaceId'
aws secretsmanager list-secrets --query 'SecretList[].Name'
aws logs describe-log-groups --log-group-name-prefix /rc-triage --query 'logGroups[].logGroupName'
aws ecr describe-repositories --query 'repositories[].repositoryName'
```

Every list should be empty (the tagging API can show recently deleted resources for a
while). Check *Billing → Bills* the next day: the charges should stop at the destroy time.
