resource "aws_iam_policy" "overly_permissive" {
  name        = "overly-permissive-s3-policy"
  description = "Grants unrestricted S3 access (intentionally insecure, for demo)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:*"
        Resource = "*"
      }
    ]
  })
}
