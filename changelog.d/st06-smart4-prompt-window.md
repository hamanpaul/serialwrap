### Fixed

- Smart4 空行 prompt 辨識使用獨立且有界的一秒回應窗，不再被小於 reader 排程週期的 RX idle 門檻縮短；已解鎖 shell 不會因過短等待誤送 unlock。PTY fixture 保留短 idle 門檻，提供完整解鎖／nonce 的總時間預算。
