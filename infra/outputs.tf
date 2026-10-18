output "instance_id" {
  value = aws_instance.api.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "rds_endpoint" {
  description = "Private address: reachable only from inside the VPC."
  value       = aws_db_instance.main.address
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "dq_lambda_name" {
  value = aws_lambda_function.dq.function_name
}

output "api_log_group" {
  value = aws_cloudwatch_log_group.api.name
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "port_forward_command" {
  description = "Run on your laptop, then call the API at http://127.0.0.1:8080 (needs the Session Manager plugin)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.api.id} --document-name AWS-StartPortForwardingSession --parameters portNumber=8080,localPortNumber=8080"
}
