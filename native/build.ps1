$ErrorActionPreference = "Stop"

$NativeDir = $PSScriptRoot
$BinDir = Join-Path $NativeDir "bin"
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null

$Gxx = "C:\msys64\ucrt64\bin\g++.exe"
$Windres = "C:\msys64\ucrt64\bin\windres.exe"

if (Test-Path $Gxx) {
    $env:PATH = "C:\msys64\ucrt64\bin;$env:PATH"
    Write-Host "[1/4] Compiling VERSIONINFO resource musicpad_apo.rc..." -ForegroundColor Cyan
    $RcFile = Join-Path $NativeDir "src\musicpad_apo.rc"
    $ResObj = Join-Path $BinDir "musicpad_apo_rc.o"
    & $Windres -i $RcFile -o $ResObj
    if ($LASTEXITCODE -ne 0) { throw "windres failed with exit code $LASTEXITCODE" }

    Write-Host "[2/4] Building musicpad_dsp.dll (shared DSP core for ctypes)..." -ForegroundColor Cyan
    $DspCpp = Join-Path $NativeDir "src\musicpad_dsp.cpp"
    $DspDll = Join-Path $BinDir "musicpad_dsp.dll"
    & $Gxx -shared -O2 -std=c++17 -Wall -Wextra -DMPAD_DSP_BUILD_DLL -static -static-libgcc -static-libstdc++ `
        $DspCpp -o $DspDll
    if ($LASTEXITCODE -ne 0) { throw "Failed to build musicpad_dsp.dll" }

    Write-Host "[3/4] Building musicpad_apo.dll (production EFX APO)..." -ForegroundColor Cyan
    $ApoCpp = Join-Path $NativeDir "src\musicpad_apo.cpp"
    $ApoDef = Join-Path $NativeDir "src\musicpad_apo.def"
    $ApoDll = Join-Path $BinDir "musicpad_apo.dll"
    & $Gxx -shared -O2 -std=c++17 -Wall -Wextra -Wno-non-virtual-dtor -static -static-libgcc -static-libstdc++ `
        $ApoCpp $DspCpp $ResObj $ApoDef -o $ApoDll -lole32 -luuid -ladvapi32 -lversion
    if ($LASTEXITCODE -ne 0) { throw "Failed to build musicpad_apo.dll" }

    Write-Host "[4/4] Building and running test_native.exe..." -ForegroundColor Cyan
    $TestCpp = Join-Path $NativeDir "tests\test_native.cpp"
    $TestExe = Join-Path $BinDir "test_native.exe"
    & $Gxx -O2 -std=c++17 -Wall -Wextra -Wno-non-virtual-dtor -static -static-libgcc -static-libstdc++ `
        $TestCpp $DspCpp -o $TestExe -lole32 -luuid -ladvapi32 -lversion
    if ($LASTEXITCODE -ne 0) { throw "Failed to build test_native.exe" }

    Push-Location $BinDir
    try {
        & $TestExe
        if ($LASTEXITCODE -ne 0) { throw "test_native.exe failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "Building via CMake..." -ForegroundColor Cyan
    $BuildDir = Join-Path $NativeDir "build"
    & cmake -S $NativeDir -B $BuildDir -A x64
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
    & cmake --build $BuildDir --config Release
    if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }
    Copy-Item "$BuildDir\Release\*.dll" $BinDir -Force -ErrorAction SilentlyContinue
    Copy-Item "$BuildDir\Release\*.exe" $BinDir -Force -ErrorAction SilentlyContinue
    Push-Location $BinDir
    try {
        & (Join-Path $BinDir "test_native.exe")
        if ($LASTEXITCODE -ne 0) { throw "test_native.exe failed" }
    } finally {
        Pop-Location
    }
}
