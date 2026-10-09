# Export .docx to PDF via WPS Writer COM — QA preview only (not part of the pack).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File convert_docx_to_pdf.ps1 -InPath <src.docx> -OutPath <dst.pdf>
param(
    [Parameter(Mandatory = $true)][string]$InPath,
    [Parameter(Mandatory = $true)][string]$OutPath
)

$ErrorActionPreference = "Stop"

$word = New-Object -ComObject KWPS.Application
try {
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $doc = $word.Documents.Open($InPath)
    try {
        $doc.ExportAsFixedFormat($OutPath, 17)
    } finally {
        $doc.Close($false)
    }
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
