param(
    [string]$Region = "eu-central-1",
    [string]$StackName = "bambi-bot-production",
    [string]$RepositoryName = "bambi-bot-production",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $key, $value = $trimmed.Split("=", 2)
        $values[$key.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $values
}

aws sts get-caller-identity --region $Region | Out-Null
$accountId = aws sts get-caller-identity --query Account --output text --region $Region

$repositoryUri = aws ecr describe-repositories `
    --repository-names $RepositoryName `
    --region $Region `
    --query "repositories[0].repositoryUri" `
    --output text 2>$null
if ($LASTEXITCODE -ne 0) {
    $repositoryUri = aws ecr create-repository `
        --repository-name $RepositoryName `
        --image-scanning-configuration scanOnPush=true `
        --image-tag-mutability IMMUTABLE `
        --region $Region `
        --query repository.repositoryUri `
        --output text
}

$lifecyclePolicy = @'
{"rules":[{"rulePriority":1,"description":"Keep the latest five production images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}
'@
aws ecr put-lifecycle-policy `
    --repository-name $RepositoryName `
    --lifecycle-policy-text $lifecyclePolicy `
    --region $Region | Out-Null

aws ecr get-login-password --region $Region |
    docker login --username AWS --password-stdin "$accountId.dkr.ecr.$Region.amazonaws.com"

$revision = (git rev-parse --short HEAD).Trim()
$tag = "$revision-$(Get-Date -Format yyyyMMddHHmmss)"
$imageUri = "${repositoryUri}:${tag}"
docker build --file Dockerfile.lambda --tag $imageUri .
docker push $imageUri

aws cloudformation deploy `
    --template-file infra/production.yaml `
    --stack-name $StackName `
    --capabilities CAPABILITY_NAMED_IAM `
    --parameter-overrides "ImageUri=$imageUri" `
    --region $Region `
    --no-fail-on-empty-changeset

$outputs = aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query "Stacks[0].Outputs" | ConvertFrom-Json
$secretArn = ($outputs | Where-Object OutputKey -eq "RuntimeSecretArn").OutputValue
$apiBaseUrl = ($outputs | Where-Object OutputKey -eq "ApiBaseUrl").OutputValue

if (Test-Path -LiteralPath $EnvFile) {
    $envValues = Read-EnvFile $EnvFile
    $currentSecret = aws secretsmanager get-secret-value `
        --secret-id $secretArn `
        --region $Region `
        --query SecretString `
        --output text | ConvertFrom-Json
    $currentSecret.anthropic_api_key = $envValues["ANTHROPIC_API_KEY"]
    $currentSecret.mybusiness_app_id = $envValues["MYBUSINESS_APP_ID"]
    $currentSecret.mybusiness_master_key = $envValues["MYBUSINESS_MASTER_KEY"]
    $currentSecret.mybusiness_base_url = $envValues["MYBUSINESS_BASE_URL"]
    $secretJson = $currentSecret | ConvertTo-Json -Compress
    $secretFile = New-TemporaryFile
    try {
        Set-Content -LiteralPath $secretFile -Value $secretJson -Encoding utf8NoBOM -NoNewline
        aws secretsmanager put-secret-value `
            --secret-id $secretArn `
            --secret-string "file://$($secretFile.FullName)" `
            --region $Region | Out-Null
    }
    finally {
        Remove-Item -LiteralPath $secretFile -Force
    }
}

$health = Invoke-RestMethod -Uri "$apiBaseUrl/health" -Method Get
if ($health.status -ne "ok") { throw "Production health check failed" }

docker logout "$accountId.dkr.ecr.$Region.amazonaws.com" | Out-Null

Write-Host "Production infrastructure is healthy."
Write-Host "Webhook URL: $(($outputs | Where-Object OutputKey -eq 'WebhookUrl').OutputValue)"
Write-Host "Runtime secret: $secretArn"
