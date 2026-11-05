---
type: fix
scope: rust-stage1
---

修正 Stage 1 出口 driver 對 `RC_SUMMARY` 的 fail-closed 判定。`l15_exit_driver.py` 未捕捉例外或摘要不是合法 JSON、缺少非負整數 `failed_steps` 時一律失敗，避免空字串被 `[[ -eq 0 ]]` 誤判通過。註解改為第二層判定：唯一核准的 `win_l15_func_test 1` 可不擋出口。
