# ดาวน์โหลดและติดตั้งส่วนประกอบของKaraokeIT
# ตัวติดตั้งเรียกทีละขั้นแบบซ่อนหน้าต่าง (-Step n -Plain) ส่วน Karaoke.bat เรียกทั้งหมดเมื่อต้องติดตั้งต่อ
param([int]$Step = 0, [switch]$Plain)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# เก็บทุกอย่างไว้ในโฟลเดอร์ที่เลือกติดตั้ง ไม่ไปกินพื้นที่ไดรฟ์ C
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot "tools\uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot "tools\python"
$env:XDG_CACHE_HOME = Join-Path $PSScriptRoot "cache"
if ($Plain) { $env:TQDM_DISABLE = "1" }
$py = ".venv\Scripts\python.exe"

# โหมด Plain ส่งข้อความเป็นภาษาอังกฤษล้วน เพราะตัวติดตั้งอ่านผ่านท่อที่แสดงภาษาไทยไม่ได้
function Say($thai, $english) { if ($Plain) { Write-Output $english } else { Write-Host $thai } }

function Run-Tool($exe, [string[]]$arguments, $what, $watch) {
    $p = Start-Process -FilePath $exe -ArgumentList $arguments -NoNewWindow -PassThru
    $null = $p.Handle
    while (-not $p.WaitForExit(10000)) {
        if ($Plain) {
            $bytes = (Get-ChildItem $watch -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
            Write-Output ("   ... downloaded {0:N0} MB so far" -f ($bytes / 1MB))
        }
    }
    if ($p.ExitCode -ne 0) { throw "$what failed" }
}

function Run-Uv([string[]]$arguments, $what) { Run-Tool "tools\uv.exe" $arguments $what "tools\uv-cache" }

function Stop-Karaoke {
    # ปิดKaraokeITที่ยังรันอยู่จากโฟลเดอร์นี้ ไม่งั้นไฟล์จะถูกล็อกและติดตั้งทับไม่ได้
    try {
        $here = $PSScriptRoot.ToLower()
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($here) -and $_.Name -like "python*") -or
            ($_.CommandLine -and $_.CommandLine.ToLower().Contains((Join-Path $here "cache\window")))
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    } catch {}
}

function Step1 {
    Say "กำลังปิดKaraokeITที่เปิดค้างอยู่ (ถ้ามี)" "Closing any running copy of Karaoke..."
    Stop-Karaoke
    Say "== 1/5 เตรียมตัวติดตั้ง ==" "Downloading the package installer (about 20 MB)..."
    New-Item -ItemType Directory -Force tools | Out-Null
    if (!(Test-Path "tools\uv.exe")) {
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile "tools\uv.zip"
        Expand-Archive "tools\uv.zip" "tools\uvtmp" -Force
        Get-ChildItem "tools\uvtmp" -Recurse -Filter "uv.exe" | Select-Object -First 1 | Copy-Item -Destination "tools\uv.exe"
        Remove-Item "tools\uv.zip", "tools\uvtmp" -Recurse -Force
    }
    Say "" "Done."
}

function Step2 {
    Say "== 2/5 ติดตั้ง Python สำหรับแอปนี้ (ไม่กระทบโปรแกรมอื่นในเครื่อง) ==" "Downloading Python 3.11 for this app only..."
    $ready = $false
    if (Test-Path $py) {
        try { & $py -c "import pip" | Out-Null; $ready = ($LASTEXITCODE -eq 0) } catch { $ready = $false }
    }
    if ($ready) {
        Say "มี Python ของแอปอยู่แล้ว ข้ามขั้นนี้" "Python for this app is already installed, skipping."
    } else {
        Run-Uv @("venv", "--seed", "--allow-existing", "--python", "3.11", ".venv") "Python setup"
    }
    Say "" "Done."
}

function Step3 {
    Say "== 3/5 ติดตั้ง AI แยกเสียงและส่วนประกอบ (ขั้นนี้นานที่สุด) ==" "Checking for an NVIDIA graphics card..."
    cmd /c "nvidia-smi >nul 2>&1"
    if ($LASTEXITCODE -eq 0) {
        Say "พบการ์ดจอ NVIDIA: จะติดตั้งแบบใช้การ์ดจอ (ประมวลผลเร็ว)" "NVIDIA card found: installing the fast GPU version (about 3 GB download)."
        Run-Uv @("pip", "install", "--python", $py, "torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu126") "PyTorch install"
    } else {
        Say "ไม่พบการ์ดจอ NVIDIA: จะติดตั้งแบบใช้ CPU (ใช้ได้ปกติ แต่ประมวลผลช้ากว่า)" "No NVIDIA card: installing the CPU version (about 300 MB download)."
        Run-Uv @("pip", "install", "--python", $py, "torch", "torchaudio") "PyTorch install"
    }
    Repair-Torch
    Say "" "Installing the vocal remover, YouTube downloader and lyric timing AI..."
    Run-Uv @("pip", "install", "--python", $py, "flask", "yt-dlp[default]", "demucs", "soundfile", "openai-whisper") "Component install"
    Say "" "Done."
}

function Test-Torch {
    # ลองเปิด PyTorch จริง ๆ ผลลัพธ์และข้อความ error เก็บไว้ที่ tools\torch-check.txt
    $p = Start-Process -FilePath $py -ArgumentList '-c', '"import torch"' -NoNewWindow -PassThru -Wait `
        -RedirectStandardError "tools\torch-check.txt" -RedirectStandardOutput "tools\torch-check-out.txt"
    return ($p.ExitCode -eq 0)
}

function Repair-Torch {
    # PyTorch เปิดไม่ขึ้นบน Windows ส่วนใหญ่เป็นเพราะเครื่องขาด Microsoft Visual C++ Redistributable (หรือเก่าเกินไป)
    if (Test-Torch) { return }
    Say "เครื่องนี้ขาดส่วนเสริม Microsoft Visual C++ กำลังติดตั้งให้ (Windows จะถามสิทธิ์ ให้กด Yes)" "PyTorch cannot start yet. Installing the Microsoft Visual C++ runtime (Windows will ask for permission, press Yes)..."
    Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile "tools\vc_redist.x64.exe"
    $r = Start-Process -FilePath "tools\vc_redist.x64.exe" -ArgumentList "/install", "/passive", "/norestart" -PassThru -Wait
    Say "" ("Visual C++ runtime installer finished with code " + $r.ExitCode)
    if (!(Test-Torch)) { throw "PyTorch still cannot start on this PC (details in tools\torch-check.txt). Restart Windows and run this installer again" }
    Say "" "PyTorch starts correctly now."
}

function Step4 {
    Say "== 4/5 ดาวน์โหลด ffmpeg ==" "Downloading ffmpeg (about 100 MB)..."
    if (!(Test-Path "ffmpeg.exe") -or !(Test-Path "ffprobe.exe")) {
        Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile "tools\ffmpeg.zip"
        Expand-Archive "tools\ffmpeg.zip" "tools\fftmp" -Force
        foreach ($name in "ffmpeg.exe", "ffprobe.exe") {
            Get-ChildItem "tools\fftmp" -Recurse -Filter $name | Select-Object -First 1 | Copy-Item -Destination $name
        }
        Remove-Item "tools\ffmpeg.zip", "tools\fftmp" -Recurse -Force
    }
    if (Test-Path "tools\uv-cache") { Remove-Item "tools\uv-cache" -Recurse -Force -ErrorAction SilentlyContinue }
    Set-Content ".venv\installed.ok" "ok"
    Say "" "Done."
}

function Step5 {
    Say "== 5/5 ดาวน์โหลดโมเดล AI (จะได้ใช้งานได้ทันที ไม่ต้องรอตอนทำเพลงแรก) ==" "Downloading the AI models so the first song starts right away..."
    try {
        Run-Tool $py @("karaoke_app.py", "--prepare") "Model download" "cache"
    } catch {
        Say "ดาวน์โหลดโมเดลไม่ครบ แอปจะโหลดส่วนที่เหลือเองตอนทำเพลงแรก" "Some models were not downloaded; the app will fetch them on first use."
    }
    Say "ติดตั้งเสร็จแล้ว" "All done."
}

try {
    if (!$Plain) {
        Write-Host "กำลังติดตั้งส่วนประกอบของKaraokeIT" -ForegroundColor Yellow
        Write-Host "ต้องต่ออินเทอร์เน็ต ใช้เวลาราว 15-30 นาที ระหว่างนี้อย่าปิดหน้าต่างนี้"
    }
    $todo = if ($Step) { @($Step) } else { 1..5 }
    foreach ($n in $todo) { & "Step$n" }
    if (!$Plain) { Start-Sleep -Seconds 2 }
}
catch {
    if ($Plain) {
        Write-Output "ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host ""
    Write-Host "ติดตั้งไม่สำเร็จ: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "เช็กอินเทอร์เน็ตและพื้นที่ว่างในเครื่อง แล้วเปิดไอคอน KaraokeITอีกครั้ง (จะทำต่อจากเดิม)"
    Read-Host "กด Enter เพื่อปิด"
}
