#requires -RunAsAdministrator
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Address, [ValidateRange(1024,65535)][int]$Port=18789)
$ErrorActionPreference='Stop'
$parsed=[System.Net.IPAddress]::Parse($Address)
$bytes=$parsed.GetAddressBytes()
if ($bytes.Length -ne 4 -or -not ($bytes[0] -eq 10 -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168))) {
    throw 'Use the explicit RFC1918 IPv4 address of the lab network adapter.'
}
if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Address -ErrorAction SilentlyContinue)) {throw 'Address is not assigned to this computer.'}
$ruleName='Erdos-CUMCM-Lab-'+$Address+'-'+$Port
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    Write-Output 'Dedicated lab rule already exists; no automatic change.'
    exit 0
}
New-NetFirewallRule -Name $ruleName -DisplayName 'Erdos CUMCM lab API' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $Address -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Select-Object Name,Enabled,Direction,Action
