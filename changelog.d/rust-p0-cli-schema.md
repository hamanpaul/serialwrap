---
type: feat
scope: contracts
---
新增 `contracts/cli_schema.json`：凍結全域選項、20 個頂層群組（含群組層選項與互斥組）的子命令與選項、每個子命令送出的 RPC 與參數組裝、stdout／stderr 格式、exit code 總表、重試白名單、預設與硬編逾時、env 變數、`--version` 與版本不符警告，以及 Stage 1A 委派集合，並以測試對照 `build_parser()` 與實際 RPC 呼叫防漂移。
