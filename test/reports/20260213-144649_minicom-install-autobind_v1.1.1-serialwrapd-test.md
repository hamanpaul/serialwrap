# minicom-install-autobind 測試報告

- 時間: 2026-02-13
- 版本: v1.1.1-serialwrapd-test
- 範圍:
  - install.sh 安裝後預設 by-id 佔位符自動綁定
  - ~/.bashrc minicom alias 指向 ~/.paul_tools/minicom_router.sh
  - 重新安裝後 minicom router 自動 attach 驗證

## 變更檔案
- /home/paul_chen/arc_prj/ser-dep/install.sh
- /home/paul_chen/.bashrc

## 驗證命令
```bash
/usr/bin/bash -n /home/paul_chen/arc_prj/ser-dep/install.sh
/home/paul_chen/arc_prj/ser-dep/install.sh /home/paul_chen/.paul_tools
/usr/bin/rg -n "alias minicom=" /home/paul_chen/.bashrc
/home/paul_chen/.paul_tools/serialwrap daemon stop
/home/paul_chen/.paul_tools/serialwrap daemon start --profile-dir /home/paul_chen/.paul_tools/profiles
MINICOM_BIN=/bin/true SERIALWRAP_ATTACH_WAIT_TICKS=160 /home/paul_chen/.paul_tools/minicom_router.sh COM0
/home/paul_chen/.paul_tools/serialwrap session list
```

## 結果
- install.sh 重裝時輸出 auto-bind:
  - [serialwrap] auto-bind default target to: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0
- ~/.paul_tools/profiles/default.yaml 已替換 `target0` 為實際 by-id。
- ~/.bashrc alias 已更新為:
  - alias minicom="/home/paul_chen/.paul_tools/minicom_router.sh"
- 實機驗證:
  - minicom_router_rc:0
  - session `COM0` 狀態為 `READY`，且有有效 `vtty`。
