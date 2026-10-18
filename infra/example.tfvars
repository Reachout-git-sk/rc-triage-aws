# Copy to terraform.tfvars (git-ignored) and fill in.
alert_email = "you@example.com"
github_repo = "Reachout-git-sk/rc-triage-aws"

# Set to false if your AWS account already has the GitHub OIDC provider
# (IAM > Identity providers > token.actions.githubusercontent.com).
create_github_oidc_provider = true
