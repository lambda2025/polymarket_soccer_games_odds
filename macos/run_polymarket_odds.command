#!/bin/bash

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

if ! cd "$PROJECT_DIR"; then
  echo "无法进入项目目录：$PROJECT_DIR"
  read -r -p "按回车键关闭此窗口..."
  exit 1
fi

if [[ ! -f ".venv/bin/activate" ]]; then
  echo "未找到虚拟环境。请先在项目目录运行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "按回车键关闭此窗口..."
  exit 1
fi

source ".venv/bin/activate"
echo "开始采集 Polymarket 英超/西甲/德甲实时赔率。"
echo "停止采集请按 Control-C；CSV 将保存在：$PROJECT_DIR"
echo

python polymarket_epl_odds.py --output polymarket_odds.csv
status=$?

deactivate
if [[ $status -eq 0 ]]; then
  echo "采集程序已结束。"
else
  echo "采集程序退出，状态码：$status"
fi
read -r -p "按回车键关闭此窗口..."
exit "$status"
