terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State stays on this laptop (git-ignored) for a short-lived practice stack.
  # A team would use an S3 backend with encryption and `use_lockfile = true`.
}

provider "aws" {
  region = var.region

  # Every resource gets these tags, which makes "is anything left?" checks easy.
  default_tags {
    tags = {
      Project   = "rc-triage-aws"
      ManagedBy = "terraform"
      Purpose   = "personal-practice-synthetic-data"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
