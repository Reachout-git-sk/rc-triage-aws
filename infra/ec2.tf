# The API host: Amazon Linux 2023 + Docker, running the API container and the fake
# GPU-metrics container. No SSH key, no inbound ports: reach it with SSM Session Manager.

data "aws_ssm_parameter" "al2023" {
  # AWS publishes the latest Amazon Linux 2023 AMI ID under this public parameter.
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${var.name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Lets the SSM agent register the instance: Session Manager and Run Command.
resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Least privilege: pull THIS repository's images, read THESE two secrets, write to
# THIS log group. Nothing else.
data "aws_iam_policy_document" "ec2_app" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this API call has no resource-level permissions
  }
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.api.arn]
  }
  statement {
    sid     = "ReadAppSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.app.arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ]
  }
  statement {
    sid       = "ContainerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ec2_app" {
  name   = "app-runtime"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ec2_app.json
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.name}-ec2"
  role = aws_iam_role.ec2.name
}

resource "aws_instance" "api" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  monitoring             = false # detailed (1-minute) EC2 metrics cost extra

  # T3 defaults to "unlimited" CPU credits, which bills extra if the CPU stays busy.
  # "standard" just slows the instance down instead: no surprise charges.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only (blocks SSRF-style credential theft)
    http_put_response_hop_limit = 1          # containers cannot reach the host's credentials
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 10
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region              = var.region
    ecr_repo_url        = aws_ecr_repository.api.repository_url
    app_secret_arn      = aws_secretsmanager_secret.app.arn
    db_admin_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn
    db_host             = aws_db_instance.main.address
    db_port             = aws_db_instance.main.port
    db_name             = aws_db_instance.main.db_name
    log_group           = aws_cloudwatch_log_group.api.name
  })
  user_data_replace_on_change = true

  tags = { Name = "${var.name}-api" }

  lifecycle {
    ignore_changes = [ami] # a newer AMI should not replace a running host by surprise
  }
}
