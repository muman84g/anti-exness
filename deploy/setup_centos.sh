#!/bin/bash
# =============================================================================
# setup_centos.sh  --  Exness MT5 Bot: CentOS 9 セットアップスクリプト
# =============================================================================
# 【変更履歴】
#   旧: Wine を CentOS に直接インストール（wine.i686 問題で動作不可）
#   新: Docker (Ubuntu コンテナ) 経由で Wine + MT5 を動かす方式に変更
# =============================================================================
# 実行方法:
#   chmod +x setup_centos.sh
#   sudo bash setup_centos.sh
# =============================================================================

set -e

echo "========================================"
echo "  Exness Bot CentOS セットアップ開始"
echo "  (Docker + Ubuntu コンテナ方式)"
echo "========================================"

# --- 1. Docker のインストール ---
echo "[1/4] Docker をインストール中..."

# Docker の公式リポジトリを追加
dnf install -y yum-utils
yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo

# Docker CE をインストール
dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Docker を起動・自動起動設定
systemctl start docker
systemctl enable docker

# 現在のユーザーを docker グループに追加（sudo なしで使えるように）
usermod -aG docker $USER
echo "      Docker インストール完了"

# --- 2. docker-compose (standalone) のインストール ---
echo "[2/4] docker-compose をインストール中..."
COMPOSE_VERSION="v2.24.0"
curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
    -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
echo "      docker-compose インストール完了: $(docker-compose --version)"

# --- 3. Git のインストール（コード取得用）---
echo "[3/4] Git をインストール中..."
dnf install -y git
echo "      Git インストール完了: $(git --version)"

# --- 4. live_config.py の配置確認 ---
echo "[4/4] live_config.py の確認..."
if [ ! -f "/app/live_config.py" ]; then
    echo ""
    echo "  ⚠️  WARNING: live_config.py が見つかりません！"
    echo "     Windows PC から SCP でコピーしてください:"
    echo "     scp live_config.py <user>@<server_ip>:/app/live_config.py"
    echo ""
else
    echo "      live_config.py 確認済み ✓"
fi

echo ""
echo "========================================"
echo "  セットアップ完了！"
echo "========================================"
echo ""
echo "次のステップ:"
echo ""
echo "  1. anti-exness リポジトリをクローン（または git pull）:"
echo "     git clone https://github.com/<あなたのユーザー名>/anti-exness.git /app"
echo ""
echo "  2. live_config.py を /app/ に手動コピー:"
echo "     scp live_config.py <user>@<server_ip>:/app/live_config.py"
echo ""
echo "  3. Docker イメージをビルド（初回は15分程度かかります）:"
echo "     cd /app && docker build -t exness-bot ."
echo ""
echo "  4. コンテナを起動:"
echo "     docker-compose up -d"
echo ""
echo "  5. ログを確認:"
echo "     docker logs -f exness-bot"
