---
type: feat
scope: rust-s21
---
Rust serialwrapd：Python 相同的參數、路徑 env 與 exit code（0/1/2/3）；啟動時先掃描、排定 COM、bootstrap attach 再啟 watcher（DF-14）；RPC 綁定後寫 config.yaml 的 socket_path（SC-09 寫入路徑）；關機先停 server、設 manager stopping gate、停 watcher、drain 全部 session（含在途 attach）、WAL sync 再釋放鎖（SC-11），未收斂時以 exit 1 與診斷回報；OD-11 allowlist 僅允許隔離實例。
