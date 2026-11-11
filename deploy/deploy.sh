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

echo "==> 코드 갱신 ($BRANCH)"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
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
if ! cmp -s deploy/nginx.conf /etc/nginx/sites-available/fastapi-stock; then
  sudo install -m 644 deploy/nginx.conf /etc/nginx/sites-available/fastapi-stock
  sudo ln -sf /etc/nginx/sites-available/fastapi-stock /etc/nginx/sites-enabled/fastapi-stock
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
  sudo systemctl reload nginx || sudo systemctl restart nginx
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
