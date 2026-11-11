#!/usr/bin/env bash
# EC2(Ubuntu)에서 실행되는 배포 스크립트. GitHub Actions가 SSH로 실행한다.
# 처음 실행할 때 필요한 패키지 설치와 저장소 clone까지 해 주므로 서버 초기 세팅에도 그대로 쓴다.
set -euo pipefail

REPO_URL="https://github.com/lws2042-ai/fastapi-stock.git"
BRANCH="${BRANCH:-main}"
APP_DIR="$HOME/fastapi-stock"
export PATH="$HOME/.local/bin:$PATH"

echo "==> 패키지 확인"
if ! command -v nginx >/dev/null || ! command -v git >/dev/null; then
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx git curl
fi
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# React 빌드 결과가 올라올 폴더 (deploy-web.sh 로 업로드)
sudo mkdir -p /var/www/fastapi-stock-react
sudo chown "$USER" /var/www/fastapi-stock-react

echo "==> 코드 갱신 ($BRANCH)"
mkdir -p "$APP_DIR"
cd "$APP_DIR"
if [ ! -d .git ]; then
  # .env 를 먼저 넣어 둔 폴더에서도 동작하도록 clone 대신 init + fetch
  git init -q
  git remote add origin "$REPO_URL"
fi
git fetch --prune origin "$BRANCH"
git reset --hard "origin/$BRANCH"   # .env 같은 무시된 파일은 지우지 않는다
git log -1 --oneline

echo "==> 의존성 설치"
uv sync --frozen --no-dev

if [ ! -f .env ]; then
  echo "⚠️  $APP_DIR/.env 가 없습니다. KIS 키를 넣어야 시세가 나옵니다." >&2
fi

echo "==> 서비스/nginx 설정 반영"
if ! cmp -s deploy/fastapi-stock.service /etc/systemd/system/fastapi-stock.service; then
  sudo install -m 644 deploy/fastapi-stock.service /etc/systemd/system/fastapi-stock.service
  sudo systemctl daemon-reload
  sudo systemctl enable fastapi-stock
fi
CERT_NAME="fastapi-stock"
CERT_DIR="/etc/letsencrypt/live/$CERT_NAME"
has_cert() { sudo test -f "$CERT_DIR/fullchain.pem"; }

# nginx 설정 설치: 인증서가 있으면 HTTPS, 없으면 HTTP 전용
install_nginx() {
  local site=deploy/nginx.conf changed=0
  has_cert && site=deploy/nginx-https.conf
  if ! cmp -s deploy/nginx-app.conf /etc/nginx/snippets/fastapi-stock-app.conf; then
    sudo install -m 644 -D deploy/nginx-app.conf /etc/nginx/snippets/fastapi-stock-app.conf; changed=1
  fi
  if ! cmp -s "$site" /etc/nginx/sites-available/fastapi-stock; then
    sudo install -m 644 "$site" /etc/nginx/sites-available/fastapi-stock; changed=1
  fi
  sudo ln -sf /etc/nginx/sites-available/fastapi-stock /etc/nginx/sites-enabled/fastapi-stock
  sudo rm -f /etc/nginx/sites-enabled/default
  if [ "$changed" = 1 ]; then
    echo "   nginx: $(basename "$site")"
    sudo nginx -t
    sudo systemctl reload nginx || sudo systemctl restart nginx
  fi
}

sudo mkdir -p /var/www/certbot
install_nginx

echo "==> HTTPS 인증서 (Let's Encrypt IP 주소 인증서)"
# IP 인증서는 certbot 5.4 이상 + shortlived(6일) 프로필이 필요하다. apt 버전은 오래돼서 snap 을 쓴다.
if ! snap list certbot >/dev/null 2>&1; then
  sudo snap install --classic certbot
fi
CERTBOT=/snap/bin/certbot
# 인증서가 갱신되면 nginx 가 새 인증서를 읽도록
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

if ! has_cert; then
  # 탄력적 IP 를 인스턴스 메타데이터(IMDSv2)에서 읽는다
  IMDS_TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
  PUBLIC_IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || true)
  if [ -n "$PUBLIC_IP" ] && sudo "$CERTBOT" certonly --webroot -w /var/www/certbot \
        --cert-name "$CERT_NAME" --ip-address "$PUBLIC_IP" --preferred-profile shortlived \
        --agree-tos --register-unsafely-without-email --non-interactive; then
    echo "   발급 완료: $PUBLIC_IP"
    install_nginx
  else
    echo "⚠️  인증서 발급 실패 — HTTP 로만 서비스합니다. (certbot $("$CERTBOT" --version 2>&1))" >&2
  fi
else
  echo "   인증서 있음 (자동 갱신: snap.certbot.renew.timer)"
fi

echo "==> 재시작"
sudo systemctl restart fastapi-stock

for i in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/docs; then
    echo "✅ 배포 완료: $(git log -1 --format='%h %s')"
    exit 0
  fi
  sleep 1
done

echo "❌ 서버가 응답하지 않습니다. 최근 로그:" >&2
sudo journalctl -u fastapi-stock -n 60 --no-pager >&2
exit 1
