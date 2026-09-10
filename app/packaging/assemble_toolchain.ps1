# Assemble the Windows host cross-toolchain bundle (app\toolchain\).
#   app\packaging\assemble_toolchain.ps1 -Mlir <dir> -Llvm <dir> -Glslang <dir> `
#       -SpirvCross <dir> -Sysroot <dir> -Out app\toolchain
# Lays out bin\{mlir-opt,mlir-translate,clang,clang++,ld.lld,glslangValidator,spirv-cross}.exe
# + sysroot\ (aarch64-linux-gnu crt + libc/libm dev) for the -shared cross link.
param(
  [Parameter(Mandatory)][string]$Mlir,
  [Parameter(Mandatory)][string]$Llvm,
  [Parameter(Mandatory)][string]$Glslang,
  [Parameter(Mandatory)][string]$SpirvCross,
  [Parameter(Mandatory)][string]$Sysroot,
  [Parameter(Mandatory)][string]$Out
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force "$Out\bin" | Out-Null
New-Item -ItemType Directory -Force "$Out\sysroot" | Out-Null

function Grab($src, $dst) {
  if (-not (Test-Path $src)) { throw "MISSING: $src" }
  Copy-Item $src "$Out\bin\$dst" -Force
}
Grab "$Mlir\mlir-opt.exe"        "mlir-opt.exe"
Grab "$Mlir\mlir-translate.exe"  "mlir-translate.exe"
Grab "$Llvm\clang.exe"           "clang.exe"
Grab "$Llvm\clang++.exe"         "clang++.exe"
Grab "$Llvm\lld.exe"             "ld.lld.exe"
Grab "$Glslang\glslangValidator.exe" "glslangValidator.exe"
Grab "$SpirvCross\spirv-cross.exe" "spirv-cross.exe"

Copy-Item "$Sysroot\*" "$Out\sysroot\" -Recurse -Force
Write-Host "==> toolchain assembled at $Out"
Get-ChildItem "$Out\bin" | Select-Object Name
