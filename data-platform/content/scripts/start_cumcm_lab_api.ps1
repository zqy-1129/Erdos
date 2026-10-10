[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Address, [string]$PythonExe='E:/Anaconda/envs/pytorch/python.exe')
$ErrorActionPreference='Stop'
$contentRoot=Split-Path -Parent $PSScriptRoot
$launchPath=Join-Path $contentRoot '.runtime/local/lab-launch.json'
if (Test-Path -LiteralPath $launchPath) {
    $launch=Get-Content -LiteralPath $launchPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $env:ERDOS_RUNTIME_DIR=$launch.runtime_directory
    $env:ERDOS_EMBEDDING_DIR=$launch.embedding_directory
}
$configPath=Join-Path $contentRoot '.runtime/local/services.json'
if ($env:ERDOS_RUNTIME_DIR) {
    $configPath=Join-Path $env:ERDOS_RUNTIME_DIR 'local/services.json'
}
$cfg=Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$directory=Join-Path $contentRoot '.runtime/local'
$statePath=Join-Path $directory 'lab-api-process.json'
$cli=Join-Path $PSScriptRoot 'clone_setup.py'
if (Test-Path -LiteralPath $statePath) {
    $state=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $old=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$state.pid)
    if ($old -and $old.CommandLine.Contains($cli) -and $old.CommandLine.Contains('--lan-host') -and $state.host -eq $Address -and $state.port -eq $cfg.api.port) {
        Write-Output ('Lab API process already exists; PID '+$state.pid+'. Verify authenticated bootstrap before use.')
        exit 0
    }
    if ($old) {throw 'State refers to a different live process; inspect it without stopping unrelated services.'}
}
# Binding validation is performed by the Python API; retain credentials only in local config.
$process=Start-Process -FilePath $PythonExe -ArgumentList @('-X','utf8',('"'+$cli+'"'),'start-api','--lan-host',$Address) -WorkingDirectory $contentRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $directory 'lab-api.stdout.log') -RedirectStandardError (Join-Path $directory 'lab-api.stderr.log') -PassThru
@{pid=$process.Id;host=$Address;port=$cfg.api.port;started_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Output ('Lab API starting at http://'+$Address+':'+$cfg.api.port+'; PID '+$process.Id+'; credentials omitted. Verify logs and authenticated bootstrap.')
