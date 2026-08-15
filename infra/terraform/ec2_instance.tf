# Gives cost_agent's pre-deployment pricing estimate something with real cost
# weight to price out - the S3 bucket + IAM policy alone are ~free.
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_instance" "demo_app_server" {
  ami           = data.aws_ami.amazon_linux.id
  instance_type = "t3.micro"

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  tags = {
    Name        = "agentic-devsecops-demo-app-server"
    Environment = "demo"
  }
}
