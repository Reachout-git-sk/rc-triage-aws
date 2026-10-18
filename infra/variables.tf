variable "region" {
  description = "AWS region for everything."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "rc-triage"
}

variable "alert_email" {
  description = "Email address that receives alarm notifications (you must click the confirmation link AWS sends)."
  type        = string
}

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to deploy, e.g. Reachout-git-sk/rc-triage-aws."
  type        = string
}

variable "github_environment" {
  description = "GitHub Environment whose jobs may assume the deploy role (it has the manual approval rule)."
  type        = string
  default     = "production"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC identity provider. Set false if the account already has one (only one per URL is allowed)."
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "instance_type" {
  description = "EC2 size for the API host. t3.micro: 2 vCPU (burstable), 1 GiB RAM."
  type        = string
  default     = "t3.micro"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "dq_schedule" {
  description = "When the data-quality Lambda runs (EventBridge Scheduler cron)."
  type        = string
  default     = "cron(0 7 * * ? *)"
}

variable "dq_schedule_timezone" {
  type    = string
  default = "America/New_York"
}

variable "error_rate_threshold_pct" {
  description = "Alarm when more than this percent of API requests in a minute are 5xx."
  type        = number
  default     = 5
}

variable "latency_p95_threshold_ms" {
  description = "Alarm when the API's p95 latency is above this for 3 minutes in a row."
  type        = number
  default     = 1000
}

variable "lambda_zip_path" {
  description = "Zip built by scripts/build_lambda.py."
  type        = string
  default     = "../build/dq_lambda.zip"
}
