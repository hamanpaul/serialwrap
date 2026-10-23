---
type: feat
scope: rust-s21
---
Rust PAL Windows 序列埠：CreateFileW 獨占、overlapped I/O 與取消收斂；只有本地取消流程中的 CancelIoEx 成功時才把 995 歸因本地取消。成功短寫或驅動逾時的部分完成會回報已確認前綴並停止，不重送餘段；每次 64-byte chunk 都經 submit gate。讀取會將 200 ms COM idle timeout 當內部輪詢，遵守呼叫端期限；DCB 對齊 pyserial，ACCESS_DENIED 以 holder_unknown 表示。取消與 USB 拔除並發時，995 來源仍無法確定；實體 USB-UART 拔除驗證維持 BLOCKED-OWNER。
