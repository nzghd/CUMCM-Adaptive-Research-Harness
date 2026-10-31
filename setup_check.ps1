$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$releaseCheck = Join-Path $PSScriptRoot "scripts\check_release.ps1"
if (-not (Test-Path -LiteralPath $releaseCheck -PathType Leaf)) {
    throw "Release check entry point not found: $releaseCheck"
}

& $releaseCheck @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
