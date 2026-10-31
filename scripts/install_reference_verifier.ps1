$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv-verifier"
$Commit = "8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770"

Write-Host "Installing llm-verifier from locked upstream commit $Commit"
if (-not (Test-Path $Venv)) {
    python -m venv $Venv
}
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --upgrade pip
& $Py -m pip install "git+https://github.com/llm-as-a-verifier/llm-as-a-verifier.git@$Commit"
& $Py -c "import llm_verifier; assert llm_verifier.__version__ == '0.2.0', llm_verifier.__version__; print('llm-verifier', llm_verifier.__version__)"
Write-Host "Installed faithful verifier environment: $Venv"
