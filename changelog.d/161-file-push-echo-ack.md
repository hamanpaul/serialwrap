---
type: fix
scope: file_transfer
---
修復 `file.push` 在無流控（`flow_control: none`）真機 console 上被節流靜默掉字（#161，#157 真機驗收揭露的更深根因——512B chunk 的 echo 於 ~73% 處斷掉）：新增 `UARTBridge.send_command_echo_paced()` 原語，把 chunk 命令行拆成 64 字元短 slice 逐段送出，每段等板端 echo 回讀確認（`_await_echo_progress()` 以 strip-ANSI＋去 CR/LF 正規化、移動起點 find 逐 slice 比對，吸收 slice 間 printk 噪音）才續送——echo 即天然的應用層流控；**全行確認後才送換行**，故 echo 停滯時命令必未執行，以 `cancel_input_line()`（Ctrl-U＋換行）清半行復原後回新錯誤碼 `TRANSFER_ECHO_STALL`（含 `chunks_sent`／`acked_chars` 診斷欄），可安全重試。`push_file()` 以 `ack_mode` 選路：`auto`（預設，bridge 支援即節流、否則 legacy 不破第三方 fake bridge）／`echo`（強制，缺原語回 `ECHO_ACK_UNSUPPORTED`）／`none`（legacy 整行送出，急件換吞吐）；CLI（`--ack-mode`）／RPC（`ack_mode`，白名單驗證）打通，#157 的 `chunk_size`／`chunk_timeout_s` 參數面與優先序完整保留。WAL 維持一命令一筆 TX。F7 回歸案：`transfer_echo_stall` 獨立 reason_code（不併一般逾時）、1MB 案 timeout 調升 1500s（echo-paced 約 10–17 分）。已知限制（範圍外、待另開 issue）：pull 側受 RX 視窗 128KiB 上限，1MB pull 仍 `PULL_PARSE_FAILED`。
