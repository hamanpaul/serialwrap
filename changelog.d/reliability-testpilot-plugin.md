---
type: feat
scope: reliability
---
`reliability/`：serialwrap-reliability testpilot plugin 殼（dev-only editable dist，永不 release）——entry point 註冊、PluginBase 生命週期映射（prepare_run＝realhw preflight gate、execute_step＝black-box `case.run(ctx)`、evaluate＝分類抄寫 `_last_failure`）、testbed.yaml 與 config.json 雙來源等價 loader、md/json reporter 重用 realhw 報告與 deployed 版本烙印；release wheel 零改動。
