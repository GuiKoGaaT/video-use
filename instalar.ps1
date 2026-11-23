# Instala o video-use (versao local: Whisper + recorte de fundo) num PC Windows.
#
# Num PC novo, num PowerShell:
#   winget install --id GitHub.cli -e
#   (feche e abra o PowerShell)
#   gh auth login --web
#   gh repo clone GuiKoGaaT/video-use $HOME\video-use
#   powershell -ExecutionPolicy Bypass -File $HOME\video-use\instalar.ps1
#
# Pode rodar de novo sem medo: o que ja estiver instalado e pulado.
# O Windows pode pedir permissao de administrador ao instalar Node, Python e Git.

$ErrorActionPreference = 'Stop'

# Repositorio de onde vem esta versao. Repo privado: faca 'gh auth login' antes.
$Repo = if ($env:VIDEO_USE_REPO) { $env:VIDEO_USE_REPO } else { 'GuiKoGaaT/video-use' }
$Dest = if ($env:VIDEO_USE_DIR) { $env:VIDEO_USE_DIR } else { Join-Path $HOME 'video-use' }
$Rvm = Join-Path $HOME 'RobustVideoMatting'
$Skills = Join-Path $HOME '.claude\skills'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    ok: $msg" -ForegroundColor Green }

# Runs a native command with all output discarded and returns its exit code.
# With ErrorActionPreference=Stop, Windows PowerShell turns redirected stderr into
# a terminating error, so relax it just for this call.
function Invoke-Quiet([scriptblock]$Block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block *> $null } catch {}
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $code
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Install-IfMissing($command, $wingetId, $label) {
    if (Get-Command $command -ErrorAction SilentlyContinue) {
        Write-Ok "$label ja instalado"
        return
    }
    Write-Host "    instalando $label ($wingetId)..."
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { throw "falha ao instalar $label" }
    Update-SessionPath
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget nao encontrado. Instale o "Instalador de Aplicativo" pela Microsoft Store e rode de novo.'
}

# ---------------------------------------------------------------- 1. base
Write-Step '1/6 Programas base'
Update-SessionPath

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $major = [int]((node -v).TrimStart('v').Split('.')[0])
    $pre = (node -v) -match '-'
    if ($major -lt 22 -or $pre) {
        Write-Host "    Node $(node -v) e antigo ou de teste; o HyperFrames pede 22+ estavel." -ForegroundColor Yellow
        Write-Host '    Desinstale-o em Configuracoes > Aplicativos e rode este script de novo.' -ForegroundColor Yellow
        throw 'Node incompativel'
    }
    Write-Ok "Node $(node -v)"
} else {
    Install-IfMissing node 'OpenJS.NodeJS.LTS' 'Node.js LTS'
}
Install-IfMissing git 'Git.Git' 'Git'
Install-IfMissing ffmpeg 'Gyan.FFmpeg' 'FFmpeg'
Install-IfMissing uv 'astral-sh.uv' 'uv'
Install-IfMissing gh 'GitHub.cli' 'GitHub CLI'

foreach ($c in 'node', 'npx', 'git', 'ffmpeg', 'ffprobe', 'uv') {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
        throw "'$c' nao esta no PATH. Feche e abra o PowerShell e rode o script de novo."
    }
}

# ---------------------------------------------------------------- 2. video-use
Write-Step "2/6 video-use em $Dest"
if (Test-Path (Join-Path $Dest '.git')) {
    git -C $Dest pull --ff-only
    Write-Ok 'atualizado'
} else {
    if ($Repo -match '^[\w.-]+/[\w.-]+$' -and (Get-Command gh -ErrorAction SilentlyContinue)) {
        # owner/name: gh usa o login do GitHub, entao funciona com repo privado
        if ((Invoke-Quiet { gh auth status }) -ne 0) {
            Write-Host '    Faca login no GitHub (abre o navegador):' -ForegroundColor Yellow
            gh auth login --hostname github.com --git-protocol https --web
        }
        gh repo clone $Repo $Dest
    } else {
        git clone $Repo $Dest
    }
    if ($LASTEXITCODE -ne 0) { throw 'falha ao baixar o video-use' }
    Write-Ok 'baixado'
}

Write-Host '    instalando dependencias Python (uv sync, alguns minutos na 1a vez)...'
Push-Location $Dest
try {
    uv sync
    if ($LASTEXITCODE -ne 0) { throw 'uv sync falhou' }
} finally { Pop-Location }
Write-Ok 'dependencias Python'

# ---------------------------------------------------------------- 3. skill
Write-Step '3/6 Registrar a skill no Claude Code'
New-Item -ItemType Directory -Force $Skills | Out-Null
$link = Join-Path $Skills 'video-use'
if (Test-Path $link) {
    Write-Ok "ja existe: $link"
} else {
    New-Item -ItemType Junction -Path $link -Target $Dest | Out-Null
    Write-Ok "$link -> $Dest"
}

# ---------------------------------------------------------------- 4. recorte de fundo
Write-Step '4/6 Recorte de fundo (Robust Video Matting)'
if (-not (Test-Path (Join-Path $Rvm 'model'))) {
    git clone --depth 1 https://github.com/PeterL1n/RobustVideoMatting $Rvm
    if ($LASTEXITCODE -ne 0) { throw 'falha ao baixar o RVM' }
}
$weights = Join-Path $Rvm 'weights\rvm_mobilenetv3.pth'
if (-not (Test-Path $weights)) {
    New-Item -ItemType Directory -Force (Split-Path $weights) | Out-Null
    curl.exe -sSL -o $weights https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3.pth
    if ($LASTEXITCODE -ne 0 -or (Get-Item $weights).Length -lt 10MB) { throw 'falha ao baixar os pesos do RVM' }
}
Write-Ok 'RVM + pesos mobilenetv3'

# ---------------------------------------------------------------- 5. HyperFrames
Write-Step '5/6 HyperFrames (skills de motion + Chrome invisivel)'
if (Test-Path (Join-Path $Skills 'hyperframes')) {
    Write-Ok 'skills ja instaladas'
} else {
    # O repo e grande demais para o clone padrao do instalador (estoura 5 min):
    # baixa so a pasta skills/ e instala dela.
    $src = Join-Path $env:TEMP 'hyperframes-src'
    if (Test-Path $src) { Remove-Item -Recurse -Force $src }
    git clone --depth 1 --filter=blob:none --no-checkout https://github.com/heygen-com/hyperframes $src
    git -C $src sparse-checkout set skills
    git -C $src checkout
    npx --yes skills add $src -g -a claude-code -y
    if ($LASTEXITCODE -ne 0) { throw 'falha ao instalar as skills do HyperFrames' }
    Remove-Item -Recurse -Force $src
    Write-Ok 'skills instaladas'
}
npx --yes hyperframes browser ensure
if ($LASTEXITCODE -ne 0) { throw 'falha ao instalar o Chrome do HyperFrames' }
Write-Ok 'Chrome invisivel pronto'

# ---------------------------------------------------------------- 6. conferir
Write-Step '6/6 Conferindo'
$py = Join-Path $Dest '.venv\Scripts\python.exe'
$helpers = Join-Path $Dest 'helpers\timeline_view.py'
if ((Invoke-Quiet { & $py $helpers --help }) -ne 0) { throw 'os helpers do video-use nao rodaram' }
if ((Invoke-Quiet { & $py -c 'import whisperx, silero_vad, torch' }) -ne 0) { throw 'WhisperX nao importou' }
Write-Ok 'helpers e WhisperX'

Write-Host ''
Write-Host 'Pronto.' -ForegroundColor Green
Write-Host 'Os modelos do Whisper (~4 GB) baixam sozinhos na primeira transcricao.'
Write-Host 'Para editar: crie uma pasta com o video (de preferencia num disco com espaco),'
Write-Host 'abra o Claude Code nela e peca a edicao.'
