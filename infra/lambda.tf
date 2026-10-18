# Daily data-quality Lambda, inside the VPC next to RDS.
#
# A Lambda in a VPC has no internet access unless you pay for a NAT gateway (~$33/month
# plus data) or VPC interface endpoints (~$7.30/month each per AZ). This design needs
# neither:
#   * DB login: an IAM auth token, which boto3 signs locally (no network call), instead
#     of reading a password from Secrets Manager (would need a Secrets Manager endpoint).
#   * Reporting: JSON lines on stdout. Lambda delivers its own logs to CloudWatch Logs
#     from outside the VPC, and the Embedded Metric Format line becomes a metric that an
#     alarm watches (calling SNS or PutMetricData directly would need endpoints).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dq_lambda" {
  name               = "${var.name}-dq-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Create/delete the network interfaces a VPC Lambda needs, and write its logs.
resource "aws_iam_role_policy_attachment" "dq_lambda_vpc" {
  role       = aws_iam_role.dq_lambda.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Log in to THIS database as THIS read-only user, and nothing else.
data "aws_iam_policy_document" "dq_lambda_db" {
  statement {
    actions = ["rds-db:connect"]
    resources = [
      "arn:${data.aws_partition.current.partition}:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.main.resource_id}/dq_reader"
    ]
  }
}

resource "aws_iam_role_policy" "dq_lambda_db" {
  name   = "rds-iam-connect"
  role   = aws_iam_role.dq_lambda.id
  policy = data.aws_iam_policy_document.dq_lambda_db.json
}

resource "aws_cloudwatch_log_group" "dq_lambda" {
  name              = "/aws/lambda/${var.name}-dq"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "dq" {
  function_name    = "${var.name}-dq"
  description      = "Daily data-quality checks on the triage database (synthetic data)"
  role             = aws_iam_role.dq_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"] # Graviton: ~20% cheaper per GB-second; the zip is pure Python
  handler          = "dq_lambda.handler.handler"
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  timeout          = 60
  memory_size      = 256

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      DB_HOST = aws_db_instance.main.address
      DB_PORT = tostring(aws_db_instance.main.port)
      DB_NAME = aws_db_instance.main.db_name
      DB_USER = "dq_reader"
    }
  }

  depends_on = [aws_cloudwatch_log_group.dq_lambda, aws_iam_role_policy_attachment.dq_lambda_vpc]
}

# ---- EventBridge Scheduler: run it once a day -----------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "invoke-dq-lambda"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.dq.arn }]
  })
}

resource "aws_scheduler_schedule" "dq_daily" {
  name                         = "${var.name}-dq-daily"
  schedule_expression          = var.dq_schedule
  schedule_expression_timezone = var.dq_schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.dq.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
