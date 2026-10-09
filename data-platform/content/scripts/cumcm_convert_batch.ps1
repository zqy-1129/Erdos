param([Parameter(Mandatory=$true)][string]$JobsPath)
$ErrorActionPreference = 'Stop'
$jobs = Get-Content -LiteralPath $JobsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$word = New-Object -ComObject KWPS.Application
try {
    $word.Visible = $false
    $word.DisplayAlerts = 0
    try { $word.AutomationSecurity = 3 } catch { }
    foreach ($job in $jobs) {
        if (Test-Path -LiteralPath $job.output) { continue }
        $doc = $null
        try {
            $doc = $word.Documents.Open($job.input, $false, $true)
            $doc.ExportAsFixedFormat($job.output, 17)
            Write-Output ('Converted ' + $job.id)
        } catch {
            Write-Output ('Conversion failed ' + $job.id + ': ' + $_.Exception.Message)
        } finally {
            if ($null -ne $doc) { $doc.Close($false) }
        }
    }
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
