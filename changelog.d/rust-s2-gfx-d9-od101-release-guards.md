---
type: fix
scope: rust-s2-gfx
---
修正 OD-101 第 9 項的 Windows parity argv 正規化與凍結集合登記；正式版 promote 驗證 RC 祖先及允許差異，同版最新 RC 驗證失敗時 fail-closed。補強 RC 封裝的四 gate 同 HEAD 證據、正式版 bundle 說明轉換與 Windows workflow_dispatch 資產防覆寫，並明確指定升級驗證 bundle 為 Stage 2。
