# Network layout (no NAT gateway, to keep the cost near zero):
#
#   VPC 10.20.0.0/16
#   +-- public subnet  10.20.1.0/24  (AZ a)  -> route 0.0.0.0/0 to the internet gateway
#   |     EC2 API host: has a public IP for OUTBOUND calls only (ECR, SSM, Secrets Manager,
#   |     CloudWatch). Its security group allows NO inbound traffic at all.
#   +-- private subnet 10.20.11.0/24 (AZ a)  -> no route to the internet
#   +-- private subnet 10.20.12.0/24 (AZ b)  -> no route to the internet
#         RDS (a DB subnet group needs two AZs) and the data-quality Lambda.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # needed to resolve the RDS endpoint name inside the VPC

  tags = { Name = "${var.name}-vpc" }
}

# The VPC's default security group allows all traffic between its members.
# Take its rules away so nothing can use it by accident.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-default-locked" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public-a" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 11 + count.index)
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.name}-private-${count.index == 0 ? "a" : "b"}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Private route table: only the built-in "local" route (inside the VPC). No internet.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
