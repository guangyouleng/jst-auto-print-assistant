[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StageDir,
    [string]$ZipPath = "",
    [switch]$ExecutableSelfTest,
    [switch]$Runtime,
    [switch]$AssertNoRunningInstance,
    [switch]$RequireAuthenticode
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$ManifestName = "SHA256SUMS.txt"
$MaxArchiveEntries = 20000
$MaxEntryBytes = 512MB
$MaxTotalBytes = 1536MB
$MaxManifestBytes = 5MB

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

function Get-SafePayloadName {
    param([string]$Name)
    Assert-True (-not [string]::IsNullOrWhiteSpace($Name)) "payload path is empty"
    Assert-True ($Name -eq $Name.Trim()) "payload path has leading or trailing whitespace: $Name"
    Assert-True (-not [IO.Path]::IsPathRooted($Name)) "payload path is rooted: $Name"
    Assert-True (-not $Name.StartsWith("/") -and -not $Name.StartsWith("\")) "payload path is rooted: $Name"
    $normalized = $Name.Replace("\", "/")
    Assert-True ($normalized -notmatch '[:\x00-\x1F]') "payload path contains an ADS separator or control character: $Name"
    $segments = @($normalized.Split('/'))
    Assert-True ($segments.Count -gt 0) "payload path is empty"
    foreach ($segment in $segments) {
        Assert-True ($segment -and $segment -ne "." -and $segment -ne "..") "payload path contains an unsafe segment: $Name"
        Assert-True (-not $segment.EndsWith(".") -and -not $segment.EndsWith(" ")) "payload path has a Windows-ambiguous suffix: $Name"
        $deviceBase = $segment.Split('.')[0]
        Assert-True ($deviceBase -notmatch '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') "payload path uses a reserved Windows device name: $Name"
    }
    Assert-True ($normalized -notmatch '(^|/)(?i:\.git|\.hg|\.svn)(/|$)') "source-control metadata is forbidden in the payload: $Name"
    Assert-True ($normalized -notmatch '(?i)(^|/)(\.env|id_rsa[^/]*|[^/]+\.(pem|key|pfx|p12|sqlite|sqlite3|db|log))$') "secret or local-state file is forbidden in the payload: $Name"
    return $normalized
}

function Get-ManifestRecords {
    param([string]$Text)
    Assert-True (-not [string]::IsNullOrWhiteSpace($Text)) "payload manifest is empty"
    $records = [Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    $lines = @($Text -split "`r?`n" | Where-Object { $_ -ne "" })
    Assert-True ($lines.Count -gt 0 -and $lines.Count -le $MaxArchiveEntries) "payload manifest entry count is outside the safety limit"
    foreach ($line in $lines) {
        $match = [regex]::Match($line, '^([0-9A-Fa-f]{64}) \*(.+)$')
        Assert-True ($match.Success) "payload manifest contains an invalid line"
        $name = Get-SafePayloadName $match.Groups[2].Value
        Assert-True ($name -ine $ManifestName) "payload manifest must not hash itself"
        Assert-True (-not $records.ContainsKey($name)) "payload manifest contains a case-insensitive duplicate path: $name"
        $records.Add($name, $match.Groups[1].Value.ToLowerInvariant())
    }
    return ,$records
}

function Get-StageRelativeName {
    param([string]$RootPath, [string]$FilePath)
    $rootPrefix = $RootPath.TrimEnd('\', '/') + '\'
    $fullPath = [IO.Path]::GetFullPath($FilePath)
    Assert-True ($fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) "staged file escaped the payload root"
    return (Get-SafePayloadName ($fullPath.Substring($rootPrefix.Length).Replace("\", "/")))
}

function Test-AuthenticodePolicy {
    param([string]$Path, [string]$Label)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($RequireAuthenticode) {
        Assert-True ([string]$signature.Status -eq "Valid") "$Label is not signed by a trusted Authenticode publisher"
    }
    elseif ([string]$signature.Status -ne "Valid") {
        Write-Host "WARNING: $Label has no trusted Authenticode signature; SHA256 detects corruption but is not publisher authentication."
    }
}

function Get-PeMachine {
    param([byte[]]$Bytes)
    Assert-True ($Bytes.Length -ge 256) "PE file is unexpectedly small"
    Assert-True ($Bytes[0] -eq 0x4D -and $Bytes[1] -eq 0x5A) "PE file is missing the MZ header"
    $peOffset = [BitConverter]::ToInt32($Bytes, 0x3C)
    Assert-True ($peOffset -ge 0x40 -and $peOffset + 6 -lt $Bytes.Length) "PE header offset is invalid"
    Assert-True (
        $Bytes[$peOffset] -eq 0x50 -and
        $Bytes[$peOffset + 1] -eq 0x45 -and
        $Bytes[$peOffset + 2] -eq 0x00 -and
        $Bytes[$peOffset + 3] -eq 0x00
    ) "PE signature is invalid"
    return [BitConverter]::ToUInt16($Bytes, $peOffset + 4)
}

function Assert-DeploymentConfig {
    param([object]$Config)
    # In StrictMode, reading an absent JSON property throws before mode checks.
    # Local configurations omit remote credentials; older remote configurations
    # omit backend_mode. Match the client's automatic mode selection.
    $propertyNames = @($Config.PSObject.Properties.Name)
    $mode = if ($propertyNames -contains "backend_mode") { [string]$Config.backend_mode } else { "auto" }
    $apiUrl = if ($propertyNames -contains "api_url") { [string]$Config.api_url } else { "" }
    $token = if ($propertyNames -contains "api_token") { [string]$Config.api_token } else { "" }
    Assert-True ($mode -in @("auto", "local", "remote")) "deployment backend_mode must be auto, local or remote"
    $localMode = ($mode -eq "local") -or ($mode -eq "auto" -and -not $apiUrl -and -not $token)
    Assert-True ($propertyNames -contains "debug_port" -and $propertyNames -contains "loop_seconds") "deployment config is missing debug_port or loop_seconds"
    $debugPort = [int]$Config.debug_port
    $loopSeconds = [int]$Config.loop_seconds
    if (-not $localMode) {
        Assert-True ($apiUrl -match '^https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+$') "deployment config must use an HTTPS API URL"
        Assert-True ($token -match '^[A-Za-z0-9_-]{32,128}$') "deployment API credential must be 32-128 ASCII letters, digits, underscores or hyphens"
    }
    Assert-True ($debugPort -ge 1024 -and $debugPort -le 65535) "deployment debug port is invalid"
    Assert-True ($loopSeconds -eq 5) "deployment loop_seconds must be 5 for V0.5.25"
}

function Test-StagePayload {
    param([string]$Root)
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $requiredRootFiles = @(
        "JSTAutoPrint_Win10_21H1.exe",
        "jst_operator_config.json",
        "试运行不打印.bat",
        "Win10_离线自检.bat",
        "Win10_后台网络诊断.bat",
        "verify_jst_win10_build.ps1",
        "使用说明_V0.5.25.md",
        "运行逻辑与流程图_V0.5.25.md",
        $ManifestName
    )
    foreach ($name in $requiredRootFiles) {
        $path = Join-Path $rootPath $name
        Assert-True (Test-Path -LiteralPath $path -PathType Leaf) "staged payload is missing $name"
        Assert-True ((Get-Item -LiteralPath $path).Length -gt 0) "staged payload contains an empty $name"
    }

    $exePath = Join-Path $rootPath "JSTAutoPrint_Win10_21H1.exe"
    $machine = Get-PeMachine ([IO.File]::ReadAllBytes($exePath))
    Assert-True ($machine -eq 0x8664) ("staged EXE machine is 0x{0:X4}, expected 0x8664 AMD64" -f $machine)

    $configPath = Join-Path $rootPath "jst_operator_config.json"
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-DeploymentConfig $config

    $manifestPath = Join-Path $rootPath $ManifestName
    Assert-True ((Get-Item -LiteralPath $manifestPath).Length -le $MaxManifestBytes) "payload manifest is unexpectedly large"
    $manifest = Get-ManifestRecords (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8)
    $allItems = @(Get-ChildItem -LiteralPath $rootPath -Recurse -Force)
    Assert-True (@($allItems).Count -le $MaxArchiveEntries) "staged payload entry count exceeds the safety limit"
    foreach ($item in $allItems) {
        Assert-True (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) "reparse points are forbidden in the staged payload: $($item.FullName)"
    }
    $allFiles = @($allItems | Where-Object { -not $_.PSIsContainer })
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [long]$totalBytes = 0
    foreach ($file in $allFiles) {
        Assert-True (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) "reparse-point files are forbidden in the staged payload"
        $relative = Get-StageRelativeName $rootPath $file.FullName
        Assert-True ($seen.Add($relative)) "staged payload contains a case-insensitive duplicate path: $relative"
        Assert-True ($file.Length -le $MaxEntryBytes) "staged file exceeds the per-entry size limit: $relative"
        $totalBytes += [long]$file.Length
        Assert-True ($totalBytes -le $MaxTotalBytes) "staged payload exceeds the total size limit"
        if ($relative -ine $ManifestName) {
            Assert-True ($manifest.ContainsKey($relative)) "staged payload contains a file not listed in the manifest: $relative"
            $actualHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            Assert-True ($actualHash -eq $manifest[$relative]) "staged payload SHA256 mismatch: $relative"
        }
    }
    Assert-True ($manifest.Count -eq ($allFiles.Count - 1)) "staged payload does not exactly match the manifest"
    foreach ($name in $manifest.Keys) {
        Assert-True ($seen.Contains($name)) "manifest lists a missing staged file: $name"
    }

    $hasPlaywrightPayload = @($allFiles | Where-Object {
        $_.FullName -match '[\\/]playwright[\\/]'
    }).Count -gt 0
    Assert-True (-not $hasPlaywrightPayload) "staged payload must not contain the retired Playwright runtime"

    Test-AuthenticodePolicy $exePath "staged EXE"
    Test-AuthenticodePolicy (Join-Path $rootPath "verify_jst_win10_build.ps1") "staged verification script"

    Write-Host ("STAGE OK: {0} manifest-verified files; {1} bytes; PE machine 0x{2:X4}" -f $manifest.Count, $totalBytes, $machine)
}

function Invoke-ExecutableSelfTest {
    param([string]$Root, [int]$TimeoutSeconds = 30)

    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $exePath = Join-Path $rootPath "JSTAutoPrint_Win10_21H1.exe"
    $outputPath = Join-Path ([IO.Path]::GetTempPath()) (
        "jst-auto-print-self-test-{0}.json" -f [Guid]::NewGuid().ToString("N")
    )
    $process = $null
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $exePath
        $startInfo.Arguments = "--self-test --self-test-output `"$outputPath`""
        $startInfo.WorkingDirectory = $rootPath
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        $started = $process.Start()
        Assert-True $started "windowed EXE self-test process could not be started"
        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $finished) {
            try {
                $process.Kill()
                $process.WaitForExit()
            }
            catch {
                Write-Host ("WARNING: timed-out self-test process could not be stopped cleanly: {0}" -f $_.Exception.Message)
            }
            throw "windowed EXE self-test timed out after $TimeoutSeconds seconds"
        }

        $exitCode = $process.ExitCode
        Assert-True (Test-Path -LiteralPath $outputPath -PathType Leaf) "windowed EXE returned exit code $exitCode and did not create --self-test-output JSON"
        Assert-True ((Get-Item -LiteralPath $outputPath).Length -gt 0) "windowed EXE created an empty --self-test-output JSON"

        try {
            $result = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            throw ("windowed EXE returned exit code {0} and its self-test output is not valid JSON: {1}" -f $exitCode, $_.Exception.Message)
        }
        $propertyNames = @($result.PSObject.Properties.Name)
        foreach ($name in @("version", "tkinter", "native_cdp", "self_test", "errors")) {
            Assert-True ($propertyNames -contains $name) "windowed EXE self-test JSON is missing '$name'"
        }
        if ($exitCode -ne 0) {
            $reportedErrors = @($result.errors) -join " | "
            if (-not $reportedErrors) {
                $reportedErrors = "none reported"
            }
            throw ("windowed EXE self-test returned exit code {0}, expected 0; self_test={1}; errors={2}" -f $exitCode, $result.self_test, $reportedErrors)
        }
        Assert-True ([string]$result.version -eq "0.5.25") "windowed EXE self-test version is not 0.5.25"
        Assert-True (($result.tkinter -is [bool]) -and $result.tkinter) "windowed EXE self-test did not confirm tkinter=true"
        Assert-True (($result.native_cdp -is [bool]) -and $result.native_cdp) "windowed EXE self-test did not confirm native_cdp=true"
        Assert-True ([string]$result.self_test -eq "PASS") "windowed EXE self-test JSON did not report self_test=PASS"
        Assert-True (@($result.errors).Count -eq 0) "windowed EXE self-test JSON reported one or more errors"

        Write-Host "WINDOWED EXE SELF-TEST OK: exit=0; version=0.5.25; tkinter=true; native_cdp=true"
        Write-Host "INFO: browser and print-service fields are diagnostics only; they are not executable self-test gates."
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
        if (Test-Path -LiteralPath $outputPath) {
            Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Read-ZipEntryBytes {
    param([object]$Entry, [long]$MaximumBytes = $MaxEntryBytes)
    Assert-True ([long]$Entry.Length -le $MaximumBytes) "ZIP entry exceeds its read limit"
    $entryStream = $Entry.Open()
    try {
        $memory = New-Object System.IO.MemoryStream
        try {
            $buffer = New-Object byte[] 65536
            while (($read = $entryStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                Assert-True (($memory.Length + $read) -le $MaximumBytes) "ZIP entry exceeded its actual read limit"
                $memory.Write($buffer, 0, $read)
            }
            Assert-True ($memory.Length -eq [long]$Entry.Length) "ZIP entry actual length does not match its declared length"
            # Prevent PowerShell from streaming a large EXE one byte at a time.
            return ,$memory.ToArray()
        }
        finally {
            $memory.Dispose()
        }
    }
    finally {
        $entryStream.Dispose()
    }
}

function Get-ZipEntrySha256 {
    param([object]$Entry, [long]$MaximumBytes = $MaxEntryBytes)
    Assert-True ([long]$Entry.Length -le $MaximumBytes) "ZIP entry exceeds its hash limit"
    $entryStream = $Entry.Open()
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $buffer = New-Object byte[] 65536
        [long]$actualBytes = 0
        while (($read = $entryStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $actualBytes += [long]$read
            Assert-True ($actualBytes -le $MaximumBytes) "ZIP entry exceeded its actual hash limit"
            [void]$sha256.TransformBlock($buffer, 0, $read, $buffer, 0)
        }
        [void]$sha256.TransformFinalBlock($buffer, 0, 0)
        Assert-True ($actualBytes -eq [long]$Entry.Length) "ZIP entry actual length does not match its declared length"
        return [PSCustomObject]@{
            Hash = ([BitConverter]::ToString($sha256.Hash)).Replace("-", "").ToLowerInvariant()
            Length = $actualBytes
        }
    }
    finally {
        $sha256.Dispose()
        $entryStream.Dispose()
    }
}

function Assert-SafeZipEntryType {
    param([object]$Entry, [bool]$IsDirectory, [string]$Name)
    $attributes = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int32]$Entry.ExternalAttributes), 0)
    $windowsAttributes = $attributes -band 0xFFFF
    Assert-True (($windowsAttributes -band [uint32][IO.FileAttributes]::ReparsePoint) -eq 0) "ZIP reparse-point entries are forbidden: $Name"
    $unixType = ($attributes -shr 16) -band 0xF000
    if ($unixType -ne 0) {
        if ($IsDirectory) {
            Assert-True ($unixType -eq 0x4000) "ZIP entry is not a regular directory: $Name"
        }
        else {
            Assert-True ($unixType -eq 0x8000) "ZIP entry is not a regular file: $Name"
        }
    }
}

function Test-ZipPayload {
    param([string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $resolvedZip = (Resolve-Path -LiteralPath $ArchivePath).Path
    $archive = [System.IO.Compression.ZipFile]::OpenRead($resolvedZip)
    try {
        Assert-True ($archive.Entries.Count -gt 0 -and $archive.Entries.Count -le $MaxArchiveEntries) "ZIP entry count is outside the safety limit"
        $entryMap = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
        $directoryNames = New-Object System.Collections.Generic.List[string]
        [long]$totalBytes = 0
        foreach ($entry in $archive.Entries) {
            $rawName = [string]$entry.FullName
            $slashName = $rawName.Replace("\", "/")
            $isDirectory = $slashName.EndsWith("/")
            $safeName = Get-SafePayloadName $(if ($isDirectory) { $slashName.TrimEnd('/') } else { $slashName })
            $mapName = if ($isDirectory) { "$safeName/" } else { $safeName }
            Assert-True (-not $entryMap.ContainsKey($mapName)) "ZIP contains a case-insensitive duplicate path: $mapName"
            Assert-True (-not $entryMap.ContainsKey($safeName) -and -not $entryMap.ContainsKey("$safeName/")) "ZIP contains a file/directory path collision: $safeName"
            Assert-SafeZipEntryType $entry $isDirectory $mapName
            $entryMap.Add($mapName, $entry)
            if ($isDirectory) {
                Assert-True ([long]$entry.Length -eq 0) "ZIP directory entry has unexpected content: $mapName"
                $directoryNames.Add($safeName)
                continue
            }
            Assert-True ([long]$entry.Length -le $MaxEntryBytes) "ZIP entry exceeds the per-entry size limit: $safeName"
            $totalBytes += [long]$entry.Length
            Assert-True ($totalBytes -le $MaxTotalBytes) "ZIP exceeds the total uncompressed size limit"
        }

        Assert-True ($entryMap.ContainsKey($ManifestName)) "ZIP payload manifest is missing"
        $manifestEntry = $entryMap[$ManifestName]
        Assert-True ([long]$manifestEntry.Length -le $MaxManifestBytes) "ZIP payload manifest is unexpectedly large"
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $manifestBytes = Read-ZipEntryBytes $manifestEntry $MaxManifestBytes
        $manifestText = $strictUtf8.GetString($manifestBytes)
        $manifest = Get-ManifestRecords $manifestText
        [long]$verifiedTotalBytes = $manifestBytes.Length

        $fileEntries = @($entryMap.Keys | Where-Object { -not $_.EndsWith("/") })
        Assert-True ($fileEntries.Count -eq ($manifest.Count + 1)) "ZIP files do not exactly match the payload manifest"
        foreach ($name in $fileEntries) {
            if ($name -ieq $ManifestName) {
                continue
            }
            Assert-True ($manifest.ContainsKey($name)) "ZIP contains a file not listed in the manifest: $name"
            $digest = Get-ZipEntrySha256 $entryMap[$name]
            $verifiedTotalBytes += [long]$digest.Length
            Assert-True ($verifiedTotalBytes -le $MaxTotalBytes) "ZIP exceeded the actual total uncompressed size limit"
            Assert-True ($digest.Hash -eq $manifest[$name]) "ZIP SHA256 mismatch: $name"
        }
        Assert-True ($verifiedTotalBytes -eq $totalBytes) "ZIP actual total length does not match its declared total length"
        foreach ($name in $manifest.Keys) {
            Assert-True ($entryMap.ContainsKey($name)) "manifest lists a missing ZIP file: $name"
            $segments = @($name.Split('/'))
            if ($segments.Count -gt 1) {
                $prefix = ""
                for ($index = 0; $index -lt ($segments.Count - 1); $index++) {
                    $prefix = if ($prefix) { "$prefix/$($segments[$index])" } else { $segments[$index] }
                    Assert-True (-not $entryMap.ContainsKey($prefix)) "ZIP file shadows a parent directory: $prefix"
                }
            }
        }
        foreach ($directoryName in $directoryNames) {
            $prefix = "$directoryName/"
            $hasChild = @($manifest.Keys | Where-Object { $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
            Assert-True $hasChild "ZIP contains an unmanifested empty directory: $directoryName"
        }

        $requiredRootFiles = @(
            "JSTAutoPrint_Win10_21H1.exe",
            "jst_operator_config.json",
            "试运行不打印.bat",
            "Win10_离线自检.bat",
            "verify_jst_win10_build.ps1",
            "使用说明_V0.5.25.md",
            "运行逻辑与流程图_V0.5.25.md",
            $ManifestName
        )
        foreach ($name in $requiredRootFiles) {
            Assert-True ($entryMap.ContainsKey($name)) "ZIP must contain exactly one root $name"
            Assert-True ([long]$entryMap[$name].Length -gt 0) "ZIP contains an empty $name"
        }

        $exeEntry = $entryMap["JSTAutoPrint_Win10_21H1.exe"]
        $machine = Get-PeMachine (Read-ZipEntryBytes $exeEntry)
        Assert-True ($machine -eq 0x8664) ("ZIP EXE machine is 0x{0:X4}, expected 0x8664 AMD64" -f $machine)

        $configEntry = $entryMap["jst_operator_config.json"]
        $configText = [Text.Encoding]::UTF8.GetString((Read-ZipEntryBytes $configEntry))
        Assert-DeploymentConfig ($configText | ConvertFrom-Json)

        $normalizedNames = @($manifest.Keys | ForEach-Object { $_.ToLowerInvariant() })
        Assert-True (@($normalizedNames | Where-Object { $_ -match '(^|/)playwright/' }).Count -eq 0) "ZIP must not contain the retired Playwright runtime"

        Write-Host ("ZIP OK: {0} entries; {1} manifest-verified files; {2} actual uncompressed bytes; EXE machine 0x{3:X4}" -f $archive.Entries.Count, $manifest.Count, $verifiedTotalBytes, $machine)
    }
    finally {
        $archive.Dispose()
    }
}

function Test-LocalTcpPort {
    param([int]$Port, [int]$TimeoutMs = 800)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Test-RuntimePrerequisites {
    Assert-True ([Environment]::Is64BitOperatingSystem) "Windows must be a 64-bit operating system"

    $browserCandidates = @(
        (Join-Path ([string]$env:PROGRAMFILES) "Google\Chrome\Application\chrome.exe"),
        (Join-Path ([string]${env:PROGRAMFILES(X86)}) "Google\Chrome\Application\chrome.exe"),
        (Join-Path ([string]$env:LOCALAPPDATA) "Google\Chrome\Application\chrome.exe"),
        (Join-Path ([string]$env:PROGRAMFILES) "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path ([string]${env:PROGRAMFILES(X86)}) "Microsoft\Edge\Application\msedge.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    Assert-True (@($browserCandidates).Count -gt 0) "Chrome or Edge was not found"

    $version = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -Method Get -TimeoutSec 3
    Assert-True ([bool]$version.webSocketDebuggerUrl) "browser port 9222 does not expose a CDP WebSocket"
    $tabs = @(Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/list" -Method Get -TimeoutSec 3)
    Assert-True (@($tabs | Where-Object { [string]$_.url -match '(^|\.)erp321\.com/' }).Count -gt 0) "no erp321.com page is open in the dedicated browser"

    $printPorts = @(@(54323, 54325) | Where-Object { Test-LocalTcpPort -Port $_ })
    Assert-True ($printPorts.Count -gt 0) "JST local print service is not listening on 54323 or 54325"

    $processCount = @(Get-Process -Name "JSTAutoPrint_Win10_21H1" -ErrorAction SilentlyContinue).Count
    Assert-True ($processCount -le 1) "more than one V0.5.25 assistant process is running"

    Write-Host ("RUNTIME LOCAL CHECK OK: browser CDP, ERP page and print port(s) {0}" -f ($printPorts -join ','))
    Write-Host ("LOCAL CLOCK: {0}; compare it with a trusted phone before the one-order acceptance" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K"))
    Write-Host "MANUAL CHECK STILL REQUIRED: one physical label, correct printer/template, readable barcode, no duplicate paper and no preship click."
}

Write-Host "CHECK: staged payload structure and AMD64 architecture"
Test-StagePayload $StageDir
if ($AssertNoRunningInstance) {
    $processCount = @(Get-Process -Name "JSTAutoPrint_Win10_21H1" -ErrorAction SilentlyContinue).Count
    Assert-True ($processCount -eq 0) "another V0.5.25 or compatibility-named assistant process is already running"
    Write-Host "SINGLE INSTANCE PRECHECK OK: no assistant process is running"
}
if ($ExecutableSelfTest) {
    Write-Host "CHECK: staged windowed EXE --self-test (no backend or order action)"
    Invoke-ExecutableSelfTest $StageDir
}
if ($ZipPath) {
    Write-Host "CHECK: delivery ZIP structure and AMD64 architecture"
    Test-ZipPayload $ZipPath
}
if ($Runtime) {
    Write-Host "CHECK: local browser/CDP page and print-service readiness"
    Test-RuntimePrerequisites
}
Write-Host "ALL REQUESTED CHECKS PASSED"
