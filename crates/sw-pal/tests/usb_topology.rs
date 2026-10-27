//! P7 Task 6（08-slice23-login-events.md）：`UsbTopology`（`transport_stall.resolve_usb_busid`）。
//! POSIX：`realpath(<sysfs_root>/class/tty/<basename(real_path)>/device)` 由深至淺第一個 fullmatch `\d+-\d+(\.\d+)*`
//! 的路徑段；Windows 恆 `None`（LOCAL-WINDOWS-TODO：`win-test.sh --package sw-pal --test usb_topology` 原生執行）。
#[cfg(unix)]
#[test]
fn busid_from_sysfs_tempdir() {
    use sw_pal::device_source::usb_posix::SysfsUsbTopology;
    use sw_pal::traits::UsbTopology;
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mk = |tty: &str, devpath: &str| {
        let dev = root.join(devpath);
        std::fs::create_dir_all(&dev).unwrap();
        let class = root.join("class/tty").join(tty);
        std::fs::create_dir_all(&class).unwrap();
        std::os::unix::fs::symlink(&dev, class.join("device")).unwrap();
    };
    mk(
        "ttyUSB0",
        "devices/platform/vhci_hcd.0/usb1/1-1/1-1:1.0/ttyUSB0",
    );
    mk(
        "ttyUSB1",
        "devices/pci0000:00/usb8/8-2/8-2.1/8-2.1:1.0/ttyUSB1",
    );
    let usb = SysfsUsbTopology::with_root(root);
    assert_eq!(usb.usb_busid("/dev/ttyUSB0").as_deref(), Some("1-1"));
    assert_eq!(usb.usb_busid("/dev/ttyUSB1").as_deref(), Some("8-2.1"));
    assert_eq!(usb.usb_busid("/dev/ttyACM9"), None, "無 symlink → None");
    assert_eq!(usb.usb_busid(""), None);
}

#[cfg(unix)]
#[test]
fn busid_resolves_existing_prefix_when_device_link_missing() {
    // 審查 c11aa89_92e0223 L-2：`os.path.realpath(strict=False)` 仍解析存在的前綴 symlink；
    // `/sys/class/tty/<name>` 本身是指向 `.../1-3/1-3:1.0/ttyUSB2/tty/<name>` 的 symlink，缺 `device` 仍得 `1-3`。
    use sw_pal::device_source::usb_posix::SysfsUsbTopology;
    use sw_pal::traits::UsbTopology;
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let dev = root.join("devices/platform/vhci_hcd.0/usb1/1-3/1-3:1.0/ttyUSB2/tty/ttyUSB2");
    std::fs::create_dir_all(&dev).unwrap();
    std::fs::create_dir_all(root.join("class/tty")).unwrap();
    std::os::unix::fs::symlink(&dev, root.join("class/tty/ttyUSB2")).unwrap();
    let usb = SysfsUsbTopology::with_root(root);
    assert_eq!(usb.usb_busid("/dev/ttyUSB2").as_deref(), Some("1-3"));
}

#[cfg(windows)]
#[test]
fn usb_windows_is_always_none() {
    use sw_pal::traits::UsbTopology;
    assert_eq!(
        sw_pal::device_source::NativeUsbTopology.usb_busid("COM3"),
        None
    );
}
