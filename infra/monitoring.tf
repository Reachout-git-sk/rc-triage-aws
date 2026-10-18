# CloudWatch: API logs, metrics made from those logs, alarms, and an email topic.

resource "aws_cloudwatch_log_group" "api" {
  name              = "/${var.name}/api"
  retention_in_days = var.log_retention_days
}

# ---- Metrics from the API's JSON request logs -----------------------------------------
# Each request writes one line like {"event": "request", "status": 200, "duration_ms": 12.3}.
# Metric filters turn matching lines into numbers; no agent or code change needed.
locals {
  api_namespace = "RcTriage/Api"
  dq_namespace  = "RcTriage/DataQuality"
}

resource "aws_cloudwatch_log_metric_filter" "requests" {
  name           = "${var.name}-requests"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"request\" }"

  metric_transformation {
    namespace = local.api_namespace
    name      = "RequestCount"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "errors_5xx" {
  name           = "${var.name}-5xx"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ ($.event = \"request\") && ($.status >= 500) }"

  metric_transformation {
    namespace = local.api_namespace
    name      = "Http5xxCount"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "latency" {
  name           = "${var.name}-latency"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.event = \"request\" }"

  metric_transformation {
    namespace = local.api_namespace
    name      = "LatencyMs"
    value     = "$.duration_ms"
    unit      = "Milliseconds"
  }
}

# ---- Where alarms send email ------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # AWS emails a confirmation link; nothing arrives until it is clicked
}

# ---- Alarms --------------------------------------------------------------------------------
# 5xx RATE, not count: 3 errors out of 3 requests is an outage, 3 out of 3,000 is noise.
# The IF(...) ignores minutes with fewer than 5 requests, so one failed request at 3 a.m.
# does not page anyone.
resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name          = "${var.name}-api-5xx-rate"
  alarm_description   = "More than ${var.error_rate_threshold_pct}% of API requests returned 5xx for 2 minutes"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.error_rate_threshold_pct
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "rate"
    expression  = "IF(requests >= 5, 100 * FILL(errors, 0) / requests, 0)"
    label       = "5xx percent"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = local.api_namespace
      metric_name = "Http5xxCount"
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = local.api_namespace
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_p95_latency" {
  alarm_name          = "${var.name}-api-p95-latency"
  alarm_description   = "API p95 latency above ${var.latency_p95_threshold_ms} ms for 3 minutes"
  namespace           = local.api_namespace
  metric_name         = "LatencyMs"
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.latency_p95_threshold_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# The checker itself broke (could not connect, code error, timeout).
resource "aws_cloudwatch_metric_alarm" "dq_lambda_errors" {
  alarm_name          = "${var.name}-dq-lambda-errors"
  alarm_description   = "The data-quality Lambda failed to run"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dq.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The checker ran and found bad data. "ignore" keeps the alarm in ALARM between daily
# runs, until a later run reports zero failed checks.
resource "aws_cloudwatch_metric_alarm" "dq_failed_checks" {
  alarm_name          = "${var.name}-dq-failed-checks"
  alarm_description   = "A daily data-quality check returned FAIL (see /aws/lambda/${var.name}-dq)"
  namespace           = local.dq_namespace
  metric_name         = "FailedChecks"
  dimensions          = { Service = "rc-triage" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "ignore"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
