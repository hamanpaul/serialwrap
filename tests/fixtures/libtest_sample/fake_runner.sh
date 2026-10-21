#!/bin/sh
# 假 runner（--runner 替身，測試會複製為 bin/fake_runner.sh）：
# 位於 exe_timeout/ 目錄下的 exe 模擬被 timeout 殺掉（exit 124）；其餘照原樣執行。
case "$1" in
  */exe_timeout/*) exit 124 ;;
esac
exec "$@"
