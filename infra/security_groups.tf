# Security groups are stateful firewalls around each resource. Rules point at other
# security groups instead of IP ranges, so "who may talk to the database" reads as
# "the API host and the Lambda", not as a list of addresses.

resource "aws_security_group" "ec2" {
  name        = "${var.name}-ec2"
  description = "API host: no inbound; outbound HTTPS and Postgres only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-ec2" }
}

resource "aws_security_group" "lambda" {
  name        = "${var.name}-dq-lambda"
  description = "Data-quality Lambda: outbound Postgres only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-dq-lambda" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-rds"
  description = "PostgreSQL: 5432 only from the API host and the Lambda"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-rds" }
}

# EC2: no ingress rules at all. Admin access is SSM Session Manager, which works
# through the SSM agent's own outbound HTTPS connection: no port 22, no SSH keys.
resource "aws_vpc_security_group_egress_rule" "ec2_https" {
  security_group_id = aws_security_group.ec2.id
  description       = "HTTPS to AWS APIs (ECR, SSM, Secrets Manager, CloudWatch) and OS packages"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "ec2_to_rds" {
  security_group_id            = aws_security_group.ec2.id
  description                  = "Postgres to RDS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_egress_rule" "lambda_to_rds" {
  security_group_id            = aws_security_group.lambda.id
  description                  = "Postgres to RDS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_ec2" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the API host"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.ec2.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_lambda" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the data-quality Lambda"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.lambda.id
}
