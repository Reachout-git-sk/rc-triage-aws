# ---- ECR: the private registry for the API image --------------------------------------
resource "aws_ecr_repository" "api" {
  name = "${var.name}-api"
  # Tags are git SHAs and can never be overwritten, so "deployed abc123" always means
  # the same bytes. Rollback = run the previous SHA again.
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # practice only: lets destroy remove a repo that has images

  image_scanning_configuration {
    scan_on_push = true # basic vulnerability scan of OS packages, free
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---- Secrets Manager: app DB password and the ingest API key ---------------------------
# Generated here so no human ever types or sees them. They do end up in the local
# Terraform state file, which is why the state is git-ignored (a team would use an
# encrypted S3 backend). The RDS admin password is NOT here: RDS manages that one.
resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "random_password" "ingest_api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/app"
  description             = "API database password and ingest API key (practice project)"
  recovery_window_in_days = 0 # practice only: delete at once on destroy
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    app_db_password = random_password.app_db.result
    ingest_api_key  = random_password.ingest_api_key.result
  })
}
