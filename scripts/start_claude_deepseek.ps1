# Delegate to the run-aware launcher. It enforces phase, budget, billing
# acknowledgement, and durable attempt/outcome logging before starting Claude.
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "tools\launch_claude_deepseek.py"
$python = $env:CUMCM_PYTHON
if (-not $python) {
  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if (-not $pythonCommand) {
    Write-Error "Python was not found. Set CUMCM_PYTHON to a Python executable."
    exit 2
  }
  $python = $pythonCommand.Source
}

& $python $launcher @args
exit $LASTEXITCODE
