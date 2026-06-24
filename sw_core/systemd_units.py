"""
serialwrap systemd unit 範本產生器。

提供兩個純函式，回傳 systemd unit 檔案內容字串，無任何 I/O 副作用。
呼叫端（T11 reconciler）負責寫檔。

設計重點：刻意省略 PrivateDevices 與 DeviceAllow。
這兩個沙箱指令會遮蔽 /dev/ttyUSB*，導致 daemon 無法存取 UART 裝置。
serialwrap 需要直接存取 /dev/ttyUSB*（及其他 /dev/tty* 節點），
故系統安全邊界由 User=serialwrap + SupplementaryGroups=dialout 實現，
不倚賴 systemd namespace 隔離 /dev。
"""


def render_user_unit(exec_start: str) -> str:
    """
    產生 user scope（~/.config/systemd/user/）的 serialwrap daemon unit 內容。

    Args:
        exec_start: ExecStart 指令字串，例如 ``%h/.local/bin/serialwrapd``。

    Returns:
        完整的 systemd unit 檔案文字（不含尾端換行）。

    注意：不含 PrivateDevices / DeviceAllow，確保 /dev/ttyUSB* 可見。
    """
    return f"""\
[Unit]
Description=serialwrap UART broker daemon (user)
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target"""


def render_system_unit(exec_start: str, run_user: str = "serialwrap") -> str:
    """
    產生 system scope（/etc/systemd/system/）的 serialwrap daemon unit 內容。

    執行身份為 ``run_user``（預設 ``serialwrap`` service account；pipx 使用者安裝
    流程會帶入「安裝者本人的帳號」，因為 serialwrapd binary 落在該使用者 venv，
    dedicated service account 讀不到其家目錄）。附加 ``dialout`` 群組以存取
    /dev/ttyUSB* 等 UART 裝置。

    Args:
        exec_start: ExecStart 指令字串，例如
            ``/home/<user>/.local/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock``。
        run_user: service 執行身份（``User=``）；WSL／pipx 使用者安裝下為安裝者本人。

    Returns:
        完整的 systemd unit 檔案文字（不含尾端換行）。

    注意：不含 PrivateDevices / DeviceAllow，確保 /dev/ttyUSB* 可見。
    """
    return f"""\
[Unit]
Description=serialwrap UART broker daemon (system)
After=multi-user.target

[Service]
Type=simple
User={run_user}
SupplementaryGroups=dialout
RuntimeDirectory=serialwrap
StateDirectory=serialwrap
ConfigurationDirectory=serialwrap
UMask=0117
Environment=SERIALWRAP_SOCKET_GROUP=dialout
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target"""
