[CmdletBinding()]
param(
    [int]$ApiPort = 0,
    [int]$WebPort = 3000,
    [switch]$BaseOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Names,
        [Parameter(Mandatory = $true)]
        [string]$DisplayName
    )

    foreach ($name in $Names) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }

    throw "Missing required dependency: $DisplayName. Install it, add it to PATH, and open a new PowerShell window."
}

function Test-TcpPortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $listener = $null
    try {
        $address = [System.Net.IPAddress]::Parse("127.0.0.1")
        $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList $address, $Port
        $listener.Start()
        return $true
    }
    catch [System.Net.Sockets.SocketException] {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Find-AvailablePort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RequestedPort,
        [Parameter(Mandatory = $true)]
        [string]$Purpose,
        [int[]]$ExcludedPorts = @()
    )

    if ($RequestedPort -lt 1 -or $RequestedPort -gt 65535) {
        throw "$Purpose port must be between 1 and 65535 (received $RequestedPort)."
    }

    $lastPort = [Math]::Min(65535, $RequestedPort + 100)
    for ($candidate = $RequestedPort; $candidate -le $lastPort; $candidate++) {
        if ($ExcludedPorts -contains $candidate) {
            continue
        }
        if (Test-TcpPortAvailable -Port $candidate) {
            return $candidate
        }
    }

    throw "No available $Purpose port was found between $RequestedPort and $lastPort."
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Start-LocalProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{}
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    foreach ($entry in $Environment.GetEnumerator()) {
        $startInfo.EnvironmentVariables[$entry.Key] = [string]$entry.Value
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Could not start $Executable."
    }
    return $process
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds
    )

    $deadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = "no response"

    while ([System.DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "$Name stopped during startup (exit code $($Process.ExitCode))."
        }

        $response = $null
        try {
            $request = [System.Net.WebRequest]::CreateHttp($Uri)
            $request.Method = "GET"
            $request.Proxy = $null
            $request.Timeout = 2000
            $request.ReadWriteTimeout = 2000
            $response = $request.GetResponse()
            $statusCode = [int]$response.StatusCode
            if ($statusCode -ge 200 -and $statusCode -lt 400) {
                return
            }
            $lastError = "HTTP $statusCode"
        }
        catch [System.Net.WebException] {
            $lastError = $_.Exception.Message
        }
        finally {
            if ($null -ne $response) {
                $response.Close()
            }
        }

        Start-Sleep -Milliseconds 250
    }

    throw "$Name did not become ready at $Uri within $TimeoutSeconds seconds. Last error: $lastError"
}

function Stop-LocalProcessTree {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$TaskkillPath
    )

    if ($null -eq $Process) {
        return
    }

    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }

        Write-Host "Stopping $Name (PID $($Process.Id))..."

        # First request a normal tree shutdown so SQLite and active jobs can close cleanly.
        & $TaskkillPath /PID $Process.Id /T 2>$null | Out-Null
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $Process.Refresh()
            if ($Process.HasExited) {
                return
            }
            Start-Sleep -Milliseconds 250
        }

        Write-Warning "$Name did not stop gracefully; forcing its process tree to close."
        & $TaskkillPath /PID $Process.Id /T /F 2>$null | Out-Null
        if (-not $Process.WaitForExit(5000)) {
            throw "$Name process tree is still running after taskkill /T /F."
        }
    }
    catch {
        Write-Warning "Could not fully stop $Name: $($_.Exception.Message)"
    }
}

$platform = [System.Environment]::OSVersion.Platform
if ($platform -ne [System.PlatformID]::Win32NT) {
    throw "scripts/dev.ps1 is the Windows launcher. Use ./scripts/dev.sh on macOS or Linux."
}

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$originalLocation = (Get-Location).Path
$apiProcess = $null
$webProcess = $null
$taskkillPath = ""
$exitCode = 0

try {
    Set-Location -LiteralPath $projectRoot

    $uvPath = Resolve-CommandPath -Names @("uv.exe", "uv") -DisplayName "uv"
    $nodePath = Resolve-CommandPath -Names @("node.exe", "node") -DisplayName "Node.js 22+"
    $pnpmPath = Resolve-CommandPath -Names @("pnpm.cmd", "pnpm.exe", "pnpm") -DisplayName "pnpm"
    $null = Resolve-CommandPath -Names @("ffmpeg.exe", "ffmpeg") -DisplayName "FFmpeg"
    $null = Resolve-CommandPath -Names @("ffprobe.exe", "ffprobe") -DisplayName "FFprobe"
    $taskkillPath = Resolve-CommandPath -Names @("taskkill.exe") -DisplayName "Windows taskkill"

    $nodeVersion = [string](& $nodePath --version)
    $nodeVersionMatch = [System.Text.RegularExpressions.Regex]::Match(
        $nodeVersion.Trim(),
        '^v?(\d+)\.'
    )
    if ($LASTEXITCODE -ne 0 -or -not $nodeVersionMatch.Success) {
        throw "Could not determine the installed Node.js version."
    }
    if ([int]$nodeVersionMatch.Groups[1].Value -lt 22) {
        throw "Node.js 22 or newer is required (found $nodeVersion)."
    }

    $packageMetadata = Get-Content -LiteralPath (Join-Path $projectRoot "package.json") -Raw |
        ConvertFrom-Json
    $packageManager = [string]$packageMetadata.packageManager
    $packageManagerMatch = [System.Text.RegularExpressions.Regex]::Match(
        $packageManager,
        '^pnpm@(\d+\.\d+\.\d+)$'
    )
    if (-not $packageManagerMatch.Success) {
        throw "package.json must declare an exact pnpm version."
    }
    $expectedPnpmVersion = $packageManagerMatch.Groups[1].Value
    $installedPnpmVersion = [string](& $pnpmPath --version)
    if ($LASTEXITCODE -ne 0 -or $installedPnpmVersion.Trim() -ne $expectedPnpmVersion) {
        throw "pnpm $expectedPnpmVersion is required. Install it with: npm install --global pnpm@$expectedPnpmVersion"
    }

    $requestedApiPort = $ApiPort
    if ($requestedApiPort -eq 0) {
        $requestedApiPort = 8000
        $configuredPort = [System.Environment]::GetEnvironmentVariable("KARAOKE_STUDIO_PORT", "Process")
        if (-not [string]::IsNullOrWhiteSpace($configuredPort)) {
            $parsedPort = 0
            if (-not [int]::TryParse($configuredPort, [ref]$parsedPort)) {
                throw "KARAOKE_STUDIO_PORT must be an integer (received '$configuredPort')."
            }
            $requestedApiPort = $parsedPort
        }
    }

    Write-Host "Preparing Python dependencies..."
    $uvSyncArguments = @("sync", "--dev", "--frozen")
    if (-not $BaseOnly) {
        $uvSyncArguments += @("--extra", "quality", "--extra", "alignment")
    }
    Invoke-CheckedCommand -Executable $uvPath -Arguments $uvSyncArguments -Description "uv sync"

    Write-Host "Preparing frontend dependencies..."
    Invoke-CheckedCommand -Executable $pnpmPath `
        -Arguments @("install", "--frozen-lockfile") `
        -Description "pnpm install"

    $pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "uv sync completed, but $pythonPath was not created."
    }

    # Probe immediately before launch to keep the port-selection race as small as possible.
    $selectedApiPort = Find-AvailablePort -RequestedPort $requestedApiPort -Purpose "API"
    $selectedWebPort = Find-AvailablePort -RequestedPort $WebPort -Purpose "web" `
        -ExcludedPorts @($selectedApiPort)
    if ($selectedApiPort -ne $requestedApiPort) {
        Write-Warning "API port $requestedApiPort is busy; using $selectedApiPort instead."
    }
    if ($selectedWebPort -ne $WebPort) {
        Write-Warning "Web port $WebPort is busy; using $selectedWebPort instead."
    }

    $apiUrl = "http://127.0.0.1:$selectedApiPort"
    $webUrl = "http://127.0.0.1:$selectedWebPort"
    $apiArguments = "-m uvicorn karaoke_studio.api:app --app-dir backend --host 127.0.0.1 --port $selectedApiPort"
    $apiEnvironment = @{
        "KARAOKE_STUDIO_PORT" = $selectedApiPort
        "KARAOKE_STUDIO_FRONTEND" = $webUrl
        "PYTHONUTF8" = "1"
    }

    $apiProcess = Start-LocalProcess `
        -Executable $pythonPath `
        -Arguments $apiArguments `
        -WorkingDirectory $projectRoot `
        -Environment $apiEnvironment
    Write-Host "Waiting for the local API..."
    Wait-HttpEndpoint -Uri "$apiUrl/api/health" -Process $apiProcess -Name "Local API" `
        -TimeoutSeconds 60

    $pnpmExtension = [System.IO.Path]::GetExtension($pnpmPath)
    if ($pnpmExtension -ieq ".cmd" -or $pnpmExtension -ieq ".bat") {
        $commandProcessor = [System.Environment]::GetEnvironmentVariable("ComSpec", "Process")
        if ([string]::IsNullOrWhiteSpace($commandProcessor)) {
            $commandProcessor = Join-Path $env:SystemRoot "System32\cmd.exe"
        }
        $escapedPnpmPath = $pnpmPath.Replace('"', '""')
        $webArguments = "/d /s /c `"`"$escapedPnpmPath`" run dev --host 127.0.0.1 --port $selectedWebPort --strictPort`""
        $webExecutable = $commandProcessor
    }
    else {
        $webArguments = "run dev --host 127.0.0.1 --port $selectedWebPort --strictPort"
        $webExecutable = $pnpmPath
    }

    $webEnvironment = @{
        "NEXT_PUBLIC_KARAOKE_API" = $apiUrl
        "NEXT_PUBLIC_SITE_ORIGIN" = $webUrl
    }
    $webProcess = Start-LocalProcess `
        -Executable $webExecutable `
        -Arguments $webArguments `
        -WorkingDirectory $projectRoot `
        -Environment $webEnvironment
    Write-Host "Waiting for the web app..."
    Wait-HttpEndpoint -Uri $webUrl -Process $webProcess -Name "Web app" -TimeoutSeconds 120

    Write-Host ""
    Write-Host "Karaoke Studio is ready: $webUrl" -ForegroundColor Green
    Write-Host "Local API:      $apiUrl"
    Write-Host "Health check:   $apiUrl/api/health"
    Write-Host "Press Ctrl+C once to stop both services."

    while ($true) {
        Start-Sleep -Milliseconds 500
        $apiProcess.Refresh()
        $webProcess.Refresh()

        if ($apiProcess.HasExited) {
            $exitCode = $apiProcess.ExitCode
            if ($exitCode -eq 0) { $exitCode = 1 }
            Write-Host "The API stopped unexpectedly (exit code $($apiProcess.ExitCode))." -ForegroundColor Red
            break
        }
        if ($webProcess.HasExited) {
            $exitCode = $webProcess.ExitCode
            if ($exitCode -eq 0) { $exitCode = 1 }
            Write-Host "The web app stopped unexpectedly (exit code $($webProcess.ExitCode))." -ForegroundColor Red
            break
        }
    }
}
catch [System.Management.Automation.PipelineStoppedException] {
    $exitCode = 130
}
catch {
    $exitCode = 1
    Write-Host "Karaoke Studio could not start: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Stop-LocalProcessTree -Process $webProcess -Name "web app" -TaskkillPath $taskkillPath
    Stop-LocalProcessTree -Process $apiProcess -Name "API" -TaskkillPath $taskkillPath
    Set-Location -LiteralPath $originalLocation
}

exit $exitCode
