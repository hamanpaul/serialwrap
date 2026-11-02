#!/bin/sh
# 假 libtest 測試執行檔（測試會把本檔複製到各樣本目錄，旁邊放 list_all.txt、list_ignored.txt、run_out.txt、exit_code）。
# 每次呼叫的參數逐行附加到同目錄 argv.log（供測試核對實際執行了哪些 run）。
# `--list [--ignored] --format terse`：輸出同目錄樣本，exit 0。
# `--ignored <filter>…`（能力 run）：同目錄有 run_cap_out.txt 時輸出該檔並以 exit 0 結束；否則同其餘參數。
# 其餘參數：輸出 run_out.txt 並以 exit_code 結束。
dir=$(dirname "$0")
echo "$*" >> "$dir/argv.log"
case " $* " in
  *" --list "*)
    case " $* " in
      *" --ignored "*) cat "$dir/list_ignored.txt" ;;
      *) cat "$dir/list_all.txt" ;;
    esac
    exit 0 ;;
  *" --ignored "*)
    if [ -f "$dir/run_cap_out.txt" ]; then
      cat "$dir/run_cap_out.txt"
      exit 0
    fi ;;
esac
cat "$dir/run_out.txt"
exit "$(cat "$dir/exit_code")"
