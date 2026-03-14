$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot "tools\piper"
$modelsDir = Join-Path $repoRoot "models\piper"
$zipPath = Join-Path $toolsDir "piper_windows_amd64.zip"
$runtimeUrl = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
$modelUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx?download=true"
$configUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx.json?download=true"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

Write-Host "Downloading Piper runtime..."
Invoke-WebRequest -Uri $runtimeUrl -OutFile $zipPath

Write-Host "Extracting Piper runtime..."
Expand-Archive -Path $zipPath -DestinationPath $toolsDir -Force

Write-Host "Downloading Piper HFC female voice model..."
Invoke-WebRequest -Uri $modelUrl -OutFile (Join-Path $modelsDir "en_US-hfc_female-medium.onnx")
Invoke-WebRequest -Uri $configUrl -OutFile (Join-Path $modelsDir "en_US-hfc_female-medium.onnx.json")

Write-Host ""
Write-Host "Piper setup complete."
Write-Host "Runtime: $repoRoot\tools\piper\piper\piper.exe"
Write-Host "Model:   $repoRoot\models\piper\en_US-hfc_female-medium.onnx"
