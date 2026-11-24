# =============================================================================
# QQ 机器人「哈基米」Windows 一键安装与配置脚本 (PowerShell 5.1 / 7+ 兼容)
# 文件路径：<安装包根目录>\installer\install.ps1
# =============================================================================

$ErrorActionPreference = "Continue"

try {
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# 启用 TLS 1.2（避免部分 Windows 10 PowerShell 5.1 默认 TLS 版本过旧导致下载失败）
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

# 定位项目根目录（本脚本位于 <项目根目录>\installer\install.ps1）
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $ProjectDir "setup_wizard.py"))) {
    if (Test-Path (Join-Path $PSScriptRoot "setup_wizard.py")) {
        $ProjectDir = $PSScriptRoot
    }
}

function Write-Info([string]$Msg) { Write-Host "[信息] $Msg" -ForegroundColor Cyan }
function Write-Ok([string]$Msg)   { Write-Host "[完成] $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "[提示] $Msg" -ForegroundColor Yellow }
function Write-Err([string]$Msg)  { Write-Host "[错误] $Msg" -ForegroundColor Red }

function Ask-YesNo {
    param(
        [string]$Prompt,
        [bool]$DefaultYes = $true
    )
    $hint = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $ans = Read-Host "$Prompt $hint"
    if ($null -eq $ans) { return $DefaultYes }
    $ans = $ans.Trim().ToLowerInvariant()
    if ($ans -eq "") { return $DefaultYes }
    if ($ans -in @("y", "yes", "是", "1")) { return $true }
    return $false
}

function Refresh-SessionPath {
    $parts = New-Object System.Collections.Generic.List[string]
    $seen = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::OrdinalIgnoreCase)

    $addDir = {
        param([string]$d)
        if ([string]::IsNullOrWhiteSpace($d)) { return }
        $trimmed = $d.Trim().TrimEnd('\')
        if ($trimmed -eq "") { return }
        if ($seen.Add($trimmed)) {
            $parts.Add($trimmed)
        }
    }

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    foreach ($raw in @($userPath, $machinePath, $env:Path)) {
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            foreach ($entry in ($raw -split ';')) {
                & $addDir $entry
            }
        }
    }

    # 补充常见默认安装目录
    $extraDirs = @(
        (Join-Path $env:LOCALAPPDATA "agy\bin"),
        (Join-Path $env:LOCALAPPDATA "Programs\antigravity"),
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin")
    )
    foreach ($d in $extraDirs) {
        if (Test-Path $d) { & $addDir $d }
    }

    $env:Path = ($parts -join ';')
}

function Test-ValidPython([string]$ExePath) {
    if ([string]::IsNullOrWhiteSpace($ExePath)) { return $false }
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { return $false }
    # 明确跳过 Microsoft Store 的空壳占位程序
    if ($ExePath -match '(?i)\\WindowsApps\\python') { return $false }
    try {
        & $ExePath -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python([bool]$PreferVenv = $true) {
    # 1. 项目内 .venv
    if ($PreferVenv) {
        $venvPy = Join-Path $ProjectDir ".venv\Scripts\python.exe"
        if (Test-ValidPython $venvPy) { return $venvPy }
    }

    # 2. Windows Python Launcher: py -3
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $resolved = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null)
            if ($resolved) {
                $firstLine = ($resolved | Select-Object -First 1).ToString().Trim()
                if (Test-ValidPython $firstLine) { return $firstLine }
            }
        } catch {}
    }

    # 3. PATH 中的 python.exe（跳过 WindowsApps 假壳）
    $cmds = Get-Command "python.exe" -All -ErrorAction SilentlyContinue
    if ($cmds) {
        foreach ($c in $cmds) {
            if (Test-ValidPython $c.Source) { return $c.Source }
        }
    }

    # 4. 扫描常见本地安装路径（按目录名倒序优先高版本）
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe"),
        (Join-Path $env:ProgramFiles "Python3*\python.exe"),
        "C:\Python3*\python.exe"
    )
    foreach ($pat in $patterns) {
        $items = Get-ChildItem -Path $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($item in $items) {
            if (Test-ValidPython $item.FullName) { return $item.FullName }
        }
    }

    return $null
}

function Find-Agy {
    $cmd = Get-Command "agy" -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path -LiteralPath $cmd.Source -PathType Leaf)) {
        return $cmd.Source
    }
    $cands = @(
        (Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe"),
        (Join-Path $env:LOCALAPPDATA "agy\bin\agy.cmd"),
        (Join-Path $env:LOCALAPPDATA "Programs\antigravity\agy.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\agy.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\agy.cmd")
    )
    foreach ($c in $cands) {
        if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
    }
    return $null
}

function Test-DockerReady {
    $dockerCmd = Get-Command "docker.exe" -ErrorAction SilentlyContinue
    if (-not $dockerCmd) { return $false }
    try {
        & $dockerCmd.Source info *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# =============================================================================
# 欢迎横幅与解压位置检查
# =============================================================================
Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "       QQ 机器人「哈基米」Windows 一键安装与配置向导" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "当前项目目录：$ProjectDir"
Write-Host ""

# 检测是否误在 ZIP 压缩包临时预览目录里直接双击运行
if ($ProjectDir -match '(?i)\\AppData\\Local\\Temp\\(Temp\d+_|Rar\$|7z|wz)') {
    Write-Warn "检测到你可能正在压缩包（ZIP）预览窗口里直接运行本脚本！"
    Write-Warn "请先右键 ZIP 压缩包选择「全部解压缩」到常规文件夹（例如 D:\QQ机器人-哈基米），再双击运行。"
    Write-Host ""
    if (-not (Ask-YesNo "仍然强制在当前临时目录继续？（不推荐）" $false)) {
        exit 1
    }
}

Refresh-SessionPath

# =============================================================================
# [1/6] 检测并安装 Python 3.9+
# =============================================================================
Write-Host "[1/6] 检测 Python 3.9+ 运行环境..." -ForegroundColor White

$SysPython = Find-Python -PreferVenv $false
if (-not $SysPython) {
    Write-Warn "未检测到可用的 Python 3.9+（已自动排除 Microsoft Store 占位壳）。"
    if (Ask-YesNo "是否立即自动安装 Python 3.12（用户级安装，无需管理员权限）？" $true) {
        $installed = $false

        # 优先使用 winget
        $wingetCmd = Get-Command "winget.exe" -ErrorAction SilentlyContinue
        if ($wingetCmd) {
            Write-Info "尝试通过 winget 安装 Python 3.12..."
            & $wingetCmd.Source install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
            Refresh-SessionPath
            $SysPython = Find-Python -PreferVenv $false
            if ($SysPython) { $installed = $true }
        }

        # winget 不可用或失败时，回退下载官方 Python 3.12.8 amd64 安装包静默安装
        if (-not $installed) {
            Write-Info "正在从 python.org 下载官方 Python 3.12.8 (64位) 安装程序..."
            $pyUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
            $pyInstaller = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
            try {
                $oldProgress = $ProgressPreference
                $ProgressPreference = "SilentlyContinue"
                Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing
                $ProgressPreference = $oldProgress

                Write-Info "下载完成，正在静默安装 Python 3.12.8（请稍候 20~60 秒）..."
                $proc = Start-Process -FilePath $pyInstaller -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0", "Include_pip=1" -Wait -PassThru
                Remove-Item -LiteralPath $pyInstaller -Force -ErrorAction SilentlyContinue
                if ($proc.ExitCode -ne 0) {
                    Write-Warn "安装程序返回退出码：$($proc.ExitCode)"
                }
                Refresh-SessionPath
                $SysPython = Find-Python -PreferVenv $false
            } catch {
                Write-Err "下载或安装 Python 失败：$($_.Exception.Message)"
            }
        }
    }
}

if (-not $SysPython) {
    Write-Err "仍未找到 Python 3.9+，无法继续安装。"
    Write-Host "请手动打开 https://www.python.org/downloads/windows/ 下载安装 Python 3.12："
    Write-Host "  安装界面底部务必勾选 [Add python.exe to PATH]，装完后重新双击「1-一键安装与配置.bat」。"
    exit 1
}

$pyVer = (& $SysPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
Write-Ok "检测到 Python $pyVer（$SysPython）"
Write-Host ""

# =============================================================================
# [2/6] 初始化项目虚拟环境 (.venv) 与可选图片组件 (Pillow)
# =============================================================================
Write-Host "[2/6] 初始化虚拟环境 (.venv) 与可选组件 Pillow..." -ForegroundColor White

$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-ValidPython $VenvPy)) {
    Write-Info "正在创建独立虚拟环境 .venv ..."
    & $SysPython -m venv $VenvDir
    if (Test-ValidPython $VenvPy) {
        Write-Ok "虚拟环境创建完成：$VenvPy"
    } else {
        Write-Warn "虚拟环境创建失败，将直接使用系统 Python：$SysPython"
        $VenvPy = $SysPython
    }
} else {
    Write-Ok "已复用现有虚拟环境：$VenvPy"
}
$PythonExe = $VenvPy

# 可选安装 Pillow（用于支持 GIF 动图表情包）
& $PythonExe -c "import PIL" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "已检测到 Pillow（支持 QQ 动图/GIF 表情包识别）"
} else {
    Write-Info "正在安装可选组件 Pillow（用于支持 QQ 动图/GIF 表情包识别）..."
    & $PythonExe -m pip install --disable-pip-version-check --default-timeout=15 --quiet Pillow *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Pillow 安装完成"
    } else {
        Write-Warn "默认 PyPI 源较慢或不可达，尝试切换清华大学镜像源安装 Pillow..."
        & $PythonExe -m pip install --disable-pip-version-check --default-timeout=15 --quiet -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn Pillow *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Pillow（清华镜像源）安装完成"
        } else {
            Write-Warn "跳过 Pillow 安装（可能处于离线状态）。不影响主功能，联网后可重新运行本脚本补装。"
        }
    }
}
Write-Host ""

# =============================================================================
# [3/6] 检测并安装 Antigravity CLI (agy) 与 Google 登录
# =============================================================================
Write-Host "[3/6] 检测 Antigravity CLI (agy) 与 Google 账号登录..." -ForegroundColor White

$AgyExe = Find-Agy
if (-not $AgyExe) {
    Write-Warn "未检测到 Antigravity CLI (agy)。机器人需要通过 agy 调用大模型。"
    if (Ask-YesNo "是否现在一键安装官方 Antigravity CLI (agy)？" $true) {
        Write-Info "正在执行官方安装命令：irm https://antigravity.google/cli/install.ps1 | iex"
        try {
            $scriptText = Invoke-RestMethod -Uri "https://antigravity.google/cli/install.ps1"
            Invoke-Expression $scriptText
        } catch {
            Write-Warn "安装 agy 时出现异常：$($_.Exception.Message)"
            Write-Warn "请确认当前网络可以访问 Google 服务（如使用代理，请先开启系统代理或 TUN 模式）。"
        }
        Refresh-SessionPath
        $AgyExe = Find-Agy
    }
}

if ($AgyExe) {
    Write-Ok "检测到 agy：$AgyExe"
    $agyCredDir = Join-Path $env:USERPROFILE ".gemini\antigravity-cli"
    $hasCred = (Test-Path $agyCredDir) -and (@(Get-ChildItem -LiteralPath $agyCredDir -ErrorAction SilentlyContinue).Count -gt 0)
    if ($hasCred) {
        Write-Ok "检测到本地已有 agy 登录目录（$agyCredDir）"
        if (Ask-YesNo "是否仍要打开 agy 检查或切换 Google 账号登录？" $false) {
            Write-Info "即将启动 agy：登录或确认完毕后，输入 /exit 或按 Ctrl+C 即可返回向导。"
            & $AgyExe
        }
    } else {
        Write-Warn "尚未检测到本地 Google 登录态（$agyCredDir）。"
        if (Ask-YesNo "是否立即启动 agy 完成 Google 账号登录？" $true) {
            Write-Host ""
            Write-Info "即将启动 agy：请在弹出的浏览器中完成 Google 账号登录！"
            Write-Info "登录成功后回到本窗口，输入 /exit 或按 Ctrl+C 即可继续下一步。"
            Write-Host ""
            & $AgyExe
        } else {
            Write-Warn "请记得在启动机器人前手动运行一次 agy 完成 Google 登录。"
        }
    }
} else {
    Write-Warn "暂未检测到 agy。你可先继续配置，稍后在 PowerShell 运行："
    Write-Host "    irm https://antigravity.google/cli/install.ps1 | iex"
}
Write-Host ""

# =============================================================================
# [4/6] 检测 NapCat (QQ 协议端) 运行环境
# =============================================================================
Write-Host "[4/6] 检测 NapCat（QQ 协议端）运行环境..." -ForegroundColor White

$DockerInstalled = [bool](Get-Command "docker.exe" -ErrorAction SilentlyContinue)
$DockerReady = Test-DockerReady

Write-Host ""
Write-Host "Windows 下运行 NapCat 支持以下两种方案（下一步配置向导中可自由选择）：" -ForegroundColor Cyan
Write-Host "  方案 A：Docker Desktop 模式" -ForegroundColor White
Write-Host "          全隔离容器运行，支持一键 docker compose up -d 自动启动，端口仅绑 127.0.0.1。"
Write-Host "  方案 B：Windows 原生 NapCat 模式（免 Docker，轻量省内存！）" -ForegroundColor White
Write-Host "          直接运行 NapCat.Shell / NapCat-Win-OneKey，无需安装 WSL2 与 Docker Desktop！"
Write-Host "          官方下载：https://github.com/NapNeko/NapCatQQ/releases"
Write-Host "          配置向导会自动生成仅监听 127.0.0.1 的安全 onebot11.json 与 webui.json。"
Write-Host ""

if ($DockerReady) {
    Write-Ok "检测到 Docker Desktop 正在运行（可直接选方案 A，也可自由选择方案 B）"
} elseif ($DockerInstalled) {
    Write-Warn "检测到已安装 Docker Desktop 但尚未启动。若选方案 A，请先打开 Docker Desktop；若选方案 B（原生 NapCat）则无需启动。"
} else {
    Write-Info "未检测到 Docker Desktop —— 强烈推荐使用「方案 B（Windows 原生 NapCat，免 Docker）」！"
}
Write-Host ""

# =============================================================================
# [5/6] 运行交互式配置向导 (setup_wizard.py)
# =============================================================================
Write-Host "[5/6] 运行交互式配置向导（配置 QQ 号、管理员与随机密钥）..." -ForegroundColor White

$WizardPath = Join-Path $ProjectDir "setup_wizard.py"
if (-not (Test-Path -LiteralPath $WizardPath)) {
    Write-Err "找不到 $WizardPath，请确认压缩包已完整解压。"
    exit 1
}

Push-Location $ProjectDir
try {
    & $PythonExe $WizardPath
    $wizardExit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($wizardExit -ne 0) {
    Write-Err "配置向导未完成或发生错误（退出码：$wizardExit），请查看上方输出。"
    exit $wizardExit
}
Write-Ok "配置向导已完成！"
Write-Host ""

# =============================================================================
# [6/6] 启动 NapCat / 复制原生配置 / 一键启动机器人
# =============================================================================
Write-Host "[6/6] 配置 NapCat 登录与启动机器人..." -ForegroundColor White

$DockerReady = Test-DockerReady
$ComposeFile = Join-Path $ProjectDir "docker-compose.napcat.yml"
$NapcatCfgDir = Join-Path $ProjectDir "napcat\config"

# 自动判断配置向导生成的是 Docker 模式（0.0.0.0）还是 Windows 原生模式（127.0.0.1）
$IsNativeMode = $false
$onebotFiles = @(Get-ChildItem -Path (Join-Path $NapcatCfgDir "onebot11*.json") -ErrorAction SilentlyContinue)
if ($onebotFiles.Count -gt 0) {
    try {
        $obJson = Get-Content -LiteralPath $onebotFiles[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $wsHosts = @($obJson.network.websocketServers | ForEach-Object { $_.host })
        if ($wsHosts.Count -gt 0 -and ($wsHosts -notcontains "0.0.0.0")) {
            $IsNativeMode = $true
        }
    } catch {}
}

# 读取 NapCat WebUI Token 以便展示
$NapcatWebuiToken = ""
$webuiJsonPath = Join-Path $NapcatCfgDir "webui.json"
if (Test-Path -LiteralPath $webuiJsonPath) {
    try {
        $wObj = Get-Content -LiteralPath $webuiJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($wObj.token) { $NapcatWebuiToken = [string]$wObj.token }
    } catch {}
}

if ((-not $IsNativeMode) -and $DockerReady -and (Test-Path -LiteralPath $ComposeFile)) {
    if (Ask-YesNo "是否立即通过 Docker 启动 NapCat 容器并打开扫码页面？" $true) {
        Write-Info "正在启动 NapCat 容器（首次启动会拉取镜像，可能需要 1~3 分钟）..."
        Push-Location $ProjectDir
        try {
            & docker compose -f $ComposeFile up -d
            $upCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($upCode -eq 0) {
            Write-Ok "NapCat 容器已启动，正在等待 WebUI 端口 6099 就绪..."
            for ($i = 0; $i -lt 15; $i++) {
                try {
                    $tcp = New-Object System.Net.Sockets.TcpClient
                    $iar = $tcp.BeginConnect("127.0.0.1", 6099, $null, $null)
                    if ($iar.AsyncWaitHandle.WaitOne(400, $false) -and $tcp.Connected) {
                        $tcp.Close()
                        break
                    }
                    $tcp.Close()
                } catch {}
                Start-Sleep -Seconds 1
            }

            if ([string]::IsNullOrWhiteSpace($NapcatWebuiToken) -and (Test-Path -LiteralPath $webuiJsonPath)) {
                try {
                    $wObj = Get-Content -LiteralPath $webuiJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($wObj.token) { $NapcatWebuiToken = [string]$wObj.token }
                } catch {}
            }

            Write-Host ""
            Write-Host "--------------------------------------------------------" -ForegroundColor Green
            Write-Host "  NapCat WebUI 地址：http://127.0.0.1:6099/webui/"
            if (-not [string]::IsNullOrWhiteSpace($NapcatWebuiToken)) {
                Write-Host "  NapCat WebUI Token：$NapcatWebuiToken" -ForegroundColor Yellow
            } else {
                Write-Host "  NapCat WebUI Token：请查看 napcat\config\webui.json 或 docker logs napcat"
            }
            Write-Host "--------------------------------------------------------" -ForegroundColor Green
            Write-Host "请在浏览器中输入上方 Token 登录，并用【机器人 QQ 小号】手机扫码登录。"
            Start-Process "http://127.0.0.1:6099/webui/"
            [void](Read-Host "手机 QQ 扫码登录成功后，回到这里按回车继续")
        } else {
            Write-Warn "启动 NapCat 容器失败，请检查 Docker Desktop 状态或网络后重试。"
        }
    }
} else {
    Write-Host ""
    Write-Host "---------------- Windows 原生 NapCat 指引 ----------------" -ForegroundColor Cyan
    Write-Host "  1. 官方下载（选择 NapCat.Shell 或 NapCat-Win-OneKey）："
    Write-Host "     https://github.com/NapNeko/NapCatQQ/releases"
    Write-Host "  2. 本项目已为你生成仅监听 127.0.0.1 的安全配置文件，位于："
    Write-Host "     $NapcatCfgDir"
    if (-not [string]::IsNullOrWhiteSpace($NapcatWebuiToken)) {
        Write-Host "  3. NapCat WebUI Token：$NapcatWebuiToken" -ForegroundColor Yellow
    }
    Write-Host "----------------------------------------------------------" -ForegroundColor Cyan
    Write-Host ""

    if (Test-Path -LiteralPath $NapcatCfgDir) {
        if (Ask-YesNo "如果你已下载并解压了 Windows 原生 NapCat，是否现在自动复制配置文件过去？" $false) {
            $rawDest = Read-Host "请粘贴（或直接拖入）你的 NapCat 根目录或 config 目录路径"
            if (-not [string]::IsNullOrWhiteSpace($rawDest)) {
                $dest = $rawDest.Trim().Trim('"').Trim("'")
                if (Test-Path -LiteralPath $dest -PathType Container) {
                    $targetCfg = $dest
                    $subCfg = Join-Path $dest "config"
                    if ((Split-Path -Leaf $dest) -ine "config") {
                        if (Test-Path -LiteralPath $subCfg -PathType Container) {
                            $targetCfg = $subCfg
                        } else {
                            # 检查 NapCat.Shell\config 或直接创建 config
                            $targetCfg = $subCfg
                            New-Item -ItemType Directory -Path $targetCfg -Force | Out-Null
                        }
                    }

                    $toCopy = @()
                    $toCopy += @(Get-ChildItem -Path (Join-Path $NapcatCfgDir "onebot11*.json") -ErrorAction SilentlyContinue)
                    if (Test-Path -LiteralPath $webuiJsonPath) {
                        $toCopy += (Get-Item -LiteralPath $webuiJsonPath)
                    }

                    foreach ($f in $toCopy) {
                        $dstFile = Join-Path $targetCfg $f.Name
                        if (Test-Path -LiteralPath $dstFile) {
                            Copy-Item -LiteralPath $dstFile -Destination "$dstFile.bak" -Force
                        }
                        Copy-Item -LiteralPath $f.FullName -Destination $dstFile -Force
                        Write-Ok "已复制 $($f.Name) -> $dstFile"
                    }
                    Write-Host ""
                    Write-Info "请先启动你的原生 NapCat 并完成机器人 QQ 扫码登录（http://127.0.0.1:6099/webui/）。"
                    [void](Read-Host "NapCat 启动并扫码完成后，回到这里按回车继续")
                } else {
                    Write-Warn "路径不存在：$dest。稍后请手动将 $NapcatCfgDir 中的 onebot11*.json 和 webui.json 复制到 NapCat 的 config 目录。"
                }
            }
        }
    }
}

# 一键启动机器人与 Web 控制台
$RunnerPath = Join-Path $ProjectDir "bot_runner.py"
$EnvPath = Join-Path $ProjectDir "bot.env"
if ((Test-Path -LiteralPath $RunnerPath) -and (Test-Path -LiteralPath $EnvPath)) {
    Write-Host ""
    if (Ask-YesNo "是否现在在后台启动 QQ 机器人并打开本地 Web 控制台？" $true) {
        Push-Location $ProjectDir
        try {
            & $PythonExe $RunnerPath start-bg
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "机器人已在后台启动！"
                & $PythonExe $RunnerPath webui open
            } else {
                Write-Warn "机器人后台启动未完全成功，请双击「6-环境体检(Doctor).bat」排查原因。"
            }
        } finally {
            Pop-Location
        }
    }
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  安装与配置流程结束！日常管理快捷脚本：" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  - 启动机器人：双击「2-启动机器人(后台).bat」"
Write-Host "  - 打开控制台：双击「3-打开Web控制台.bat」（http://127.0.0.1:8765）"
Write-Host "  - 停止全部：  双击「5-停止全部服务.bat」"
Write-Host "  - 环境体检：  双击「6-环境体检(Doctor).bat」"
Write-Host "  - 前台调试：  双击「前台调试启动.bat」"
Write-Host ""
exit 0
