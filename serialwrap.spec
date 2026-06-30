# serialwrap.spec — PyInstaller one-file 打包（serialwrapd + serialwrap），內嵌 sw_core/assets
# 用法：pyinstaller serialwrap.spec
# 產出：dist/serialwrapd.exe、dist/serialwrap.exe（Windows one-file）
#
# hiddenimports 說明：
#   winreg  — Windows 登錄檔存取（sw_core 在 Windows 動態 import）
#   msvcrt  — Windows C 執行期函式（msvcrt.kbhit 等，console I/O）
#   serial  — pyserial（_PySerialPort 後端，Windows UART，#84 PORT-1）
#   yaml    — PyYAML（config 載入）

import os

# 內嵌 sw_core/assets 目錄到打包內的 sw_core/assets
datas = [("sw_core/assets", "sw_core/assets")]

# ---------- serialwrapd ----------
a_d = Analysis(
    ["sw_core/daemon.py"],
    datas=datas,
    hiddenimports=["winreg", "msvcrt", "serial", "yaml"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz_d = PYZ(a_d.pure)
exe_d = EXE(
    pyz_d,
    a_d.scripts,
    a_d.binaries,
    a_d.datas,
    name="serialwrapd",
    console=True,
    onefile=True,
)

# ---------- serialwrap（CLI）----------
a_c = Analysis(
    ["sw_core/cli.py"],
    datas=datas,
    hiddenimports=["winreg", "msvcrt", "serial", "yaml"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz_c = PYZ(a_c.pure)
exe_c = EXE(
    pyz_c,
    a_c.scripts,
    a_c.binaries,
    a_c.datas,
    name="serialwrap",
    console=True,
    onefile=True,
)
