# Convert legacy .doc to .docx via WPS Writer COM (KWPS.Application).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File convert_doc_to_docx.ps1 -InPath <src.doc> -OutPath <dst.docx>
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
        $wdFormatXMLDocument = 12
        try {
            $doc.SaveAs2($OutPath, $wdFormatXMLDocument)
        } catch {
            $doc.SaveAs($OutPath, $wdFormatXMLDocument)
        }
    } finally {
        $doc.Close($false)
    }
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
