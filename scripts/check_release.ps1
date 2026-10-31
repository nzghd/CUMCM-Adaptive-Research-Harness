param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$preferredPython = "E:\Anaconda\envs\common_env\python.exe"

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    if (Test-Path -LiteralPath $preferredPython -PathType Leaf) {
        $PythonExe = $preferredPython
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $PythonExe = $pythonCommand.Source
    }
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}

function Invoke-HarnessCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "==> $Label"
    & $PythonExe -B @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Push-Location -LiteralPath $repoRoot
try {
    Invoke-HarnessCheck "runtime mirror consistency" @("tools/runtime.py", "check")
    Invoke-HarnessCheck "static schema registry" @("tools/schema_validate.py", "--static")
    Invoke-HarnessCheck "run-scoped path safety regression" @("tools/self_test_path_safety.py")
    Invoke-HarnessCheck "selection identity contract regression" @("tools/self_test_selection_contract.py")
    Invoke-HarnessCheck "diversity receipt contract regression" @("tools/self_test_diversity_contract.py")
    Invoke-HarnessCheck "experiment environment boundary regression" @("tools/self_test_experiment_env.py")
    Invoke-HarnessCheck "experiment receipt replay regression" @("tools/self_test_experiment_receipt_contract.py")
    Invoke-HarnessCheck "external egress transaction regression" @("tools/self_test_egress_policy.py")
    Invoke-HarnessCheck "consistency contract regression" @("tools/self_test_consistency_contract.py")
    Invoke-HarnessCheck "human identity contract regression" @("tools/self_test_human_identity_contract.py")
    Invoke-HarnessCheck "human Gate review summary regression" @("tools/self_test_gate_review_unit.py")
    Invoke-HarnessCheck "Gate review CLI and approval binding regression" @("tools/self_test_gate_review.py")
    Invoke-HarnessCheck "G7 submission compliance regression" @("tools/self_test_submission_compliance.py")
    Invoke-HarnessCheck "official support-package boundary regression" @("tools/self_test_support_package.py")
    Invoke-HarnessCheck "release manifest isolated regression" @("tools/release_manifest.py", "self-test")
    Invoke-HarnessCheck "base compatibility regression" @("tools/self_test.py")
    Invoke-HarnessCheck "v5.1 lifecycle regression" @("tools/self_test_v51.py")
    Invoke-HarnessCheck "release manifest integrity" @("tools/release_manifest.py", "check")
}
finally {
    Pop-Location
}

Write-Host "CUMCM Harness offline release checks passed. No network/model/API call was made."
