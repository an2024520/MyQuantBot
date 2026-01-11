#!/bin/bash

# ========================================================
#  MyQuantBot 一键部署脚本 (Debian/Ubuntu)
# ========================================================

# --- 1. 配置区域 (请修改这里) ---
# 你的 GitHub 仓库地址 (公开库 HTTPS 地址)
REPO_URL="https://github.com/an2024520/MyQuantBot.git"

# 部署目录 (通常放在 /opt 下)
APP_DIR="/opt/MyQuantBot"

# 入口文件 (如果你重构了就填 run.py，如果是旧版就填 main.py)
ENTRY_FILE="run.py" 

# 服务名称
SERVICE_NAME="myquant"

# ========================================================

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
  echo "❌ 请使用 root 权限运行此脚本 (sudo bash setup.sh)"
  exit 1
fi

echo ">>> 🚀 开始部署 MyQuantBot..."

# --- 2. 系统更新与基础工具安装 ---
echo ">>> [1/6] 更新系统并安装基础工具..."
apt-get update -y
apt-get install -y git python3 python3-pip python3-venv curl

# --- 3. 拉取代码 ---
echo ">>> [2/6] 拉取代码..."
# 如果目录存在，先备份
if [ -d "$APP_DIR" ]; then
    echo "    检测到旧目录，正在备份..."
    mv "$APP_DIR" "${APP_DIR}_backup_$(date +%s)"
fi

# 克隆仓库
git clone "$REPO_URL" "$APP_DIR"
if [ $? -ne 0 ]; then
    echo "❌ 代码拉取失败，请检查 GitHub 地址是否正确。"
    exit 1
fi

# --- 4. 创建虚拟环境 (venv) ---
echo ">>> [3/6] 创建 Python 虚拟环境..."
cd "$APP_DIR"
python3 -m venv venv

# --- 5. 安装依赖 ---
echo ">>> [4/6] 安装依赖包 (这可能需要几分钟)..."
# 激活虚拟环境并安装
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r requirements.txt

# --- 6. 配置 Systemd 开机自启服务 ---
echo ">>> [5/6] 配置系统服务 (Systemd)..."

# 生成服务文件
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=MyQuantBot Trading System
After=network.target

[Service]
# 指定用户 (root 简单直接，生产环境建议专用用户)
User=root
Group=root

# 工作目录
WorkingDirectory=${APP_DIR}

# 启动命令 (使用虚拟环境中的 Python)
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/${ENTRY_FILE}

# 自动重启设置
Restart=always
RestartSec=5

# 日志输出 (直接由 Systemd 接管)
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 重载守护进程
systemctl daemon-reload

# --- 7. 启动服务 ---
echo ">>> [6/6] 启动服务并设置开机自启..."
systemctl enable ${SERVICE_NAME}
systemctl start ${SERVICE_NAME}

echo "========================================================"
echo "✅ 部署完成！"
echo "--------------------------------------------------------"
echo "🔍 查看状态: systemctl status ${SERVICE_NAME}"
echo "📜 查看日志: journalctl -u ${SERVICE_NAME} -f"
echo "🛑 停止服务: systemctl stop ${SERVICE_NAME}"
echo "🔄 重启服务: systemctl restart ${SERVICE_NAME}"
echo "========================================================"