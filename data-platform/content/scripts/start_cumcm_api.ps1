$ErrorActionPreference = 'Stop'
$contentRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $contentRoot '.runtime/local'
$statePath = Join-Path $runtimeDirectory 'api-process.json'
$pythonPath = 'E:/Anaconda/envs/pytorch/python.exe'
if (Test-Path -LiteralPath $statePath) {
    $oldState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $oldProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$oldState.pid)
    if ($oldProcess -and $oldProcess.ExecutablePath -eq 'E:\Anaconda\envs\pytorch\python.exe' -and $oldProcess.CommandLine.Contains('-m cumcm_delivery.api')) {
        Stop-Process -Id $oldProcess.ProcessId
    }
}
$env:PYTHONPATH = $PSScriptRoot
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','cumcm_delivery.api') -WorkingDirectory $contentRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDirectory 'api.stdout.log') -RedirectStandardError (Join-Path $runtimeDirectory 'api.stderr.log') -PassThru
@{pid=$process.Id;host='127.0.0.1';port=18789;started_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Output ('Local content API starting at 127.0.0.1:18789; PID ' + $process.Id + '; credentials omitted')
