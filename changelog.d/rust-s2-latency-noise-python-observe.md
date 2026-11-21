---
type: fix
scope: tests
---
`test_login_with_delay_and_noise` 的 Python 基線輪改為只觀察（擁有者裁決 2026-11-21）：Python 0.4.x 在每 50 ms 一行雜訊下會回 `*_PROMPT_TIMEOUT` 並自動重探，可能到期限仍未收斂，結果取決於主機排程；現在未收斂時發出警告而不判失敗，但整段期間出現的錯誤碼必須全部屬於登入逾時類，其他錯誤照樣失敗。Rust 輪維持必須 READY。
