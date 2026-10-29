---
type: feat
scope: release
---

release workflow 新增 `publish-linux-bundle` job（RELEASE-TODO-01）：tag push 時於 `publish` 之後以 `cargo build --release` 與 `mkbundle.sh --release-bundle --profile release` 建出 Linux x86_64 Rust bundle，打包為 `serialwrap-<VERSION>-linux-x86_64.tar.gz` 並產生 `SHA256SUMS`，解開後 smoke（`--version` 等於 `VERSION`、`--help`、release manifest 版本一致、內層雜湊核對、不含 test-fakes 故障注入鉤子）通過才以 `gh release upload` 附加；tag、`VERSION` 與 Cargo workspace 版號不一致即失敗。既有 wheel 與 PyInstaller exe job 不變；Windows bundle 由擁有者 runner 建置後附加（見 `docs/rust-refactor/release-0.4.0-plan.md` RELEASE-TODO-01）。
