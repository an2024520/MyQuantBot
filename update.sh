#!/bin/bash

# ==========================================
#  MyQuantBot 一键更新脚本 (Force Update)
# ==========================================

# 你的项目目录 (如果是默认安装，不用改)
APP_DIR="/opt/MyQuantBot"
SERVICE_NAME="myquant"

echo ">>> 📦 开始更新 MyQuantBot..."

# 1. 进入目录
if [ ! -d "$APP_DIR" ]; then
    echo "❌ 错误: 找不到目录 $APP_DIR"
    exit 1
fi
cd "$APP_DIR"

# 2. 强制同步 GitHub 代码 (会丢弃 VPS 本地的临时修改)
echo ">>> [1/3] 拉取最新代码 (Git Pull)..."
git fetch --all
# 强制重置为远程的 main 分支 (如果你的是 master，请改为 origin/master)
git reset --hard origin/main 
git pull

# 3. 重新安装依赖 (防止你新增了库)
echo ">>> [2/3] 检查并更新依赖..."
./venv/bin/pip install -r requirements.txt

# 4. 重启服务
echo ">>> [3/3] 重启服务..."
systemctl restart $SERVICE_NAME

echo "=========================================="
echo "✅ 更新完成！服务已重启。"
echo "📜 查看日志: journalctl -u $SERVICE_NAME -f"
echo "=========================================="
chmod +x /opt/MyQuantBot/update.sh