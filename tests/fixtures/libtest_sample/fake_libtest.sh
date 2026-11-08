#!/bin/sh
# 假 libtest 測試執行檔（測試會把本檔複製到各樣本目錄，旁邊放 list_all.txt、list_ignored.txt、run_out.txt、exit_code）。
# `--list [--ignored] --format terse`：輸出同目錄樣本，exit 0。
# 其餘參數：輸出 run_out.txt 並以 exit_code 結束。
dir=$(dirname "$0")
case " $* " in
  *" --list "*)
    case " $* " in
      *" --ignored "*) cat "$dir/list_ignored.txt" ;;
      *) cat "$dir/list_all.txt" ;;
    esac
    exit 0 ;;
esac
cat "$dir/run_out.txt"
exit "$(cat "$dir/exit_code")"
