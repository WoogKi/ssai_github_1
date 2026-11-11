param(
    [ValidateSet('Plan', 'Preflight', 'Run')]
    [string]$Mode = 'Plan',
    [string]$PythonExe,
    [string]$ModelKey,
    [string]$ApiBase = 'http://127.0.0.1:1234'
)

$ErrorActionPreference = 'Stop'
$diagnostics = 'D:\SSAI_DATA\diagnostics\mcp_native_phase1'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Mode -ne 'Plan' -and [IO.Path]::GetFullPath($scriptDir).TrimEnd('\') -ne $diagnostics) {
    throw "Run only from $diagnostics"
}
if (-not $PythonExe -or $PythonExe -notmatch '^[A-Za-z]:[\\/]' -or
    -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Pass the absolute path of the existing 2nd-host Python executable with -PythonExe.'
}
if ($Mode -ne 'Plan' -and -not $ModelKey) {
    throw 'Pass an already loaded model key with -ModelKey.'
}
$checker = Join-Path $scriptDir 'mcp_native_2ho_verify.py'
$server = Join-Path $scriptDir 'native_auth_poc_server.py'
if (-not (Test-Path -LiteralPath $server)) {
    $server = Join-Path $scriptDir 'mcp_servers\native_auth_poc_server.py'
}
if (-not (Test-Path -LiteralPath $checker) -or -not (Test-Path -LiteralPath $server)) {
    throw 'Both Python PoC files must be beside this script.'
}
$oldNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'
try {
    $modeArg = switch ($Mode) { 'Plan' {'plan'} 'Preflight' {'preflight'} 'Run' {'run'} }
    $arguments = @($checker, $modeArg)
    if ($Mode -ne 'Plan') {
        $arguments += @('--api-base', $ApiBase, '--model-key', $ModelKey)
    }
    & $PythonExe @arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Native MCP check stopped with exit code $code; inspect the newest diagnostics JSON. No automatic retry."
    }
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $oldNoBytecode
}
