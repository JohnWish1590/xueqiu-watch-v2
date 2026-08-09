#!/bin/sh
# 雪哨 v2 一键启动（QNAP QTS「任务排程 → 开机执行」调用此脚本）
# cd 到脚本自身所在目录运行，避免依赖调用方的工作目录
cd "$(dirname "$0")" || exit 1

# 避免重复启动（BusyBox 兼容：用 ps+grep 而非 pgrep）
if ps | grep -q '[m]ain.py'; then
  echo "雪哨已在运行，跳过"
  exit 0
fi

PY=/share/CACHEDEV1_DATA/.qpkg/Python3/opt/python3/bin/python3.12
# setsid 彻底脱离启动它的 SSH 会话，避免会话结束被一起杀掉
setsid "$PY" -u main.py >> xw.log 2>&1 < /dev/null &
echo "雪哨已启动 pid $!"
