def test_user_unit_has_restart_and_no_device_sandbox():
    from sw_core.systemd_units import render_user_unit
    text = render_user_unit(exec_start="%h/.local/bin/serialwrapd")
    assert "[Service]" in text and "[Install]" in text
    assert "ExecStart=%h/.local/bin/serialwrapd" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    # 嚴禁會擋 /dev 的沙箱指令（此類工具最常見踩雷）
    assert "PrivateDevices" not in text
    assert "DeviceAllow" not in text

def test_system_unit_runs_service_account_in_dialout():
    from sw_core.systemd_units import render_system_unit
    text = render_system_unit(exec_start="/usr/local/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock")
    assert "User=serialwrap" in text
    assert "SupplementaryGroups=dialout" in text
    assert "RuntimeDirectory=serialwrap" in text
    assert "StateDirectory=serialwrap" in text
    assert "ConfigurationDirectory=serialwrap" in text
    assert "ExecStart=/usr/local/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=multi-user.target" in text
    assert "PrivateDevices" not in text and "DeviceAllow" not in text

def test_exec_start_is_parameterized():
    from sw_core.systemd_units import render_user_unit
    assert "ExecStart=/custom/path/serialwrapd" in render_user_unit(exec_start="/custom/path/serialwrapd")


def test_system_unit_run_user_parameterized():
    """#76：system unit 的 User= 可帶安裝者本人帳號（run-as-user，pipx 使用者安裝）。"""
    from sw_core.systemd_units import render_system_unit
    text = render_system_unit(exec_start="/opt/serialwrap/serialwrapd", run_user="svcuser")
    assert "User=svcuser" in text
    assert "User=serialwrap" not in text
    assert "SupplementaryGroups=dialout" in text
    assert "ExecStart=/opt/serialwrap/serialwrapd" in text
