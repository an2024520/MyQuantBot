#!/bin/bash

# ==========================================
#  MyQuantBot 密钥配置向导 (交互式)
#  配置存储路径: /opt/myquant_config/secrets.py
# ==========================================

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then 
  echo "❌ 请使用 root 权限运行 (sudo bash setup_keys.sh)"
  exit 1
fi

CONFIG_DIR="/opt/myquant_config"
CONFIG_FILE="$CONFIG_DIR/secrets.py"

echo "========================================"
echo "   🔐 MyQuantBot 安全密钥配置向导"
echo "========================================"

# 1. 选择交易所
echo "请选择你的主力交易所:"
echo "  1) Binance (币安)"
echo "  2) OKX (欧易)"
read -p "输入数字 (1/2): " EXCHANGE_CHOICE

if [ "$EXCHANGE_CHOICE" == "2" ]; then
    EXCHANGE_ID="okx"
    echo -e "\n>>> 已选择: OKX"
else
    EXCHANGE_ID="binance"
    echo -e "\n>>> 已选择: Binance"
fi

# 2. 输入密钥 (输入时不回显，保护隐私)
echo -e "\n----------------------------------------"
read -p "请输入 Access Key (API Key): " API_KEY
read -s -p "请输入 Secret Key (私钥): " SECRET_KEY
echo ""

if [ "$EXCHANGE_ID" == "okx" ]; then
    read -s -p "请输入 Passphrase (口令): " PASSWORD
    echo ""
else
    PASSWORD=""
fi

# 3. 生成配置文件
echo -e "\n----------------------------------------"
echo ">>> 正在创建配置目录: $CONFIG_DIR ..."
mkdir -p "$CONFIG_DIR"

echo ">>> 正在写入密钥舱..."
cat > "$CONFIG_FILE" <<EOF
# MyQuantBot External Secrets
# Created at: $(date)

HARDCODED_KEYS = {
    'exchange_id': '${EXCHANGE_ID}',
    'apiKey': '${API_KEY}',
    'secret': '${SECRET_KEY}',
    'password': '${PASSWORD}'
}
EOF

# 4. 设置权限 (仅 root 可读写)
chmod 600 "$CONFIG_FILE"

echo "✅ 配置成功！密钥已安全存储在: $CONFIG_FILE"
echo "🚀 现在你可以运行 restart 脚本启动机器人了。"