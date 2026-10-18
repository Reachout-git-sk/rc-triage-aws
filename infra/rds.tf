# PostgreSQL 16 on RDS: private, encrypted, small.
#
# * publicly_accessible = false and private subnets: there is no path from the internet.
# * manage_master_user_password: RDS creates the admin password itself and keeps it in
#   Secrets Manager. Terraform never sees it, so it is not in the state file either.
# * iam_database_authentication_enabled: lets the Lambda log in with a 15-minute IAM
#   token instead of a stored password.
# * rds.force_ssl = 1: the server refuses unencrypted connections.
# * Settings marked "practice only" make `terraform destroy` fast and complete. In a real
#   system you would keep deletion protection and a final snapshot.

resource "aws_db_subnet_group" "main" {
  name       = "${var.name}-db"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${var.name}-db" }
}

resource "aws_db_parameter_group" "pg16" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true # AWS-managed KMS key (aws/rds): no extra cost

  db_name                     = "triage"
  username                    = "triage_admin"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  publicly_accessible    = false
  multi_az               = false

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  apply_immediately                   = true
  performance_insights_enabled        = false

  backup_retention_period = 1     # practice only: one day of automated backups
  skip_final_snapshot     = true  # practice only
  deletion_protection     = false # practice only

  tags = { Name = "${var.name}-db" }
}
