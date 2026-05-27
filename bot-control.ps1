<#
.SYNOPSIS
  korail-hunt 텔레그램 봇 백그라운드 실행/관리 스크립트 (Windows PowerShell).

.DESCRIPTION
  venv 의 pythonw.exe 로 bot.py 를 콘솔 창 없이 백그라운드 실행한다.
  PID 는 .bot.pid 에, 로그는 bot.log 에 기록된다. PowerShell 세션을 닫아도
  봇은 계속 실행되지만 로그오프하면 종료된다 (Windows 사용자 프로세스 제약).
  로그오프 후에도 살리려면 NSSM 으로 서비스 등록을 고려하라.

.PARAMETER Command
  start    : 백그라운드 실행
  stop     : 중단
  restart  : 중단 후 재시작
  status   : 실행 상태 + 리소스 사용량
  logs     : bot.log 를 실시간으로 출력 (Ctrl+C 로 종료)

.EXAMPLE
  .\bot-control.ps1 start
  .\bot-control.ps1 logs
  .\bot-control.ps1 restart
#>
[CmdletBinding()]
param(
  [Parameter(Position=0)]
  [ValidateSet('start','stop','restart','status','logs')]
  [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root '.bot.pid'
$LogFile = Join-Path $Root 'bot.log'

# venv 의 pythonw 우선 (콘솔 없음). 없으면 python.exe fallback.
$VenvPythonw = Join-Path $Root 'venv\Scripts\pythonw.exe'
$VenvPython  = Join-Path $Root 'venv\Scripts\python.exe'
if (Test-Path $VenvPythonw)      { $Exe = $VenvPythonw; $UsingPythonw = $true }
elseif (Test-Path $VenvPython)   { $Exe = $VenvPython;  $UsingPythonw = $false }
else                              { $Exe = $null }

function Get-RunningPid {
  if (-not (Test-Path $PidFile)) { return $null }
  $raw = Get-Content $PidFile -ErrorAction SilentlyContinue
  if (-not $raw) { return $null }
  $botPid = $raw.Trim() -as [int]
  if (-not $botPid) { return $null }
  $proc = Get-Process -Id $botPid -ErrorAction SilentlyContinue
  if (-not $proc) {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    return $null
  }
  # PID 재활용 방지: 프로세스 이름이 python* 인지 확인
  if ($proc.ProcessName -notlike 'python*') {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    return $null
  }
  return $botPid
}

function Start-Bot {
  $existing = Get-RunningPid
  if ($existing) {
    Write-Host "이미 실행 중 (PID $existing). status 로 확인." -ForegroundColor Yellow
    return
  }
  if (-not $Exe) {
    Write-Host "venv 가 없다. 다음을 먼저 실행:" -ForegroundColor Yellow
    Write-Host "  python -m venv venv"
    Write-Host "  .\venv\Scripts\Activate.ps1"
    Write-Host "  pip install -e '.[bot]'"
    return
  }
  if (-not (Test-Path (Join-Path $Root 'bot.py'))) {
    Write-Host "bot.py 가 현재 폴더에 없다: $Root" -ForegroundColor Red
    return
  }

  # 자식 프로세스가 파일 로그를 쓰도록 환경변수로 전달
  $env:BOT_LOG_FILE = $LogFile

  $startArgs = @{
    FilePath         = $Exe
    ArgumentList     = 'bot.py'
    WorkingDirectory = $Root
    PassThru         = $true
  }
  if (-not $UsingPythonw) {
    # python.exe 일 경우 콘솔 창 숨김
    $startArgs.WindowStyle = 'Hidden'
  }
  $proc = Start-Process @startArgs

  $proc.Id | Out-File -FilePath $PidFile -Encoding ascii
  Write-Host "✓ 봇 시작 (PID $($proc.Id))" -ForegroundColor Green
  Write-Host "  실행: $Exe bot.py"
  Write-Host "  로그: $LogFile"
  Write-Host "  관리: .\bot-control.ps1 {start|stop|restart|status|logs}"
  if (-not $UsingPythonw) {
    Write-Host "  주의: venv 에 pythonw.exe 가 없어 python.exe 로 실행됨 (숨김 콘솔). pip install --upgrade pip 후 venv 재생성 권장." -ForegroundColor Yellow
  }
}

function Stop-Bot {
  $botPid = Get-RunningPid
  if (-not $botPid) {
    Write-Host "실행 중 아님"
    return
  }
  Write-Host "중단 중 (PID $botPid)..."
  Stop-Process -Id $botPid -Force -ErrorAction SilentlyContinue
  Remove-Item $PidFile -ErrorAction SilentlyContinue
  Write-Host "✓ 중단됨" -ForegroundColor Green
}

function Get-Status {
  $botPid = Get-RunningPid
  if (-not $botPid) {
    Write-Host "● 실행 중 아님" -ForegroundColor Red
    return
  }
  $proc = Get-Process -Id $botPid
  $ram = [math]::Round($proc.WorkingSet64 / 1MB, 1)
  $uptime = (Get-Date) - $proc.StartTime
  Write-Host "● 실행 중" -ForegroundColor Green
  Write-Host "  PID:    $botPid"
  Write-Host "  이름:   $($proc.ProcessName)"
  Write-Host "  RAM:    $ram MB"
  Write-Host "  시작:   $($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))"
  Write-Host ("  가동:   {0}일 {1}시 {2}분" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
  Write-Host "  로그:   $LogFile"
}

function Show-Logs {
  if (-not (Test-Path $LogFile)) {
    Write-Host "로그 없음: $LogFile" -ForegroundColor Yellow
    Write-Host "봇이 한 번도 안 떴거나 BOT_LOG_FILE 미설정 상태로 실행됐을 가능성."
    return
  }
  # bot.py 는 UTF-8 로 로그를 쓴다. PowerShell 5.1 의 Get-Content 는
  # 기본적으로 시스템 코드페이지(CP949) 로 읽기 때문에 한글이 깨진다.
  # -Encoding UTF8 명시로 강제.
  # 콘솔 출력도 UTF-8 로 맞춘다 (Write-Host 가 mojibake 되는 것 방지).
  $prevOutputEncoding = [Console]::OutputEncoding
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
  try {
    Write-Host "[$LogFile] 마지막 100줄부터 tail (Ctrl+C 로 종료)" -ForegroundColor Cyan
    Get-Content $LogFile -Tail 100 -Wait -Encoding UTF8
  } finally {
    [Console]::OutputEncoding = $prevOutputEncoding
  }
}

switch ($Command) {
  'start'   { Start-Bot }
  'stop'    { Stop-Bot }
  'restart' { Stop-Bot; Start-Sleep -Seconds 2; Start-Bot }
  'status'  { Get-Status }
  'logs'    { Show-Logs }
}
