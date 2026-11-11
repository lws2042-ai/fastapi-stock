# fastapi-stock

한국투자증권 KIS Open API로 국내·해외 주식 시세, 순위, 보조지표(MACD·RSI·스토캐스틱·볼린저)와 매수/매도 신호를 제공하는 FastAPI 서버.

## 로컬 실행

```powershell
copy .env.example .env   # KIS_APP_KEY, KIS_APP_SECRET 입력
uv run fastapi dev main.py
```

- 화면: http://127.0.0.1:8000
- API 문서: http://127.0.0.1:8000/docs

## 배포 (AWS EC2)

`main` 브랜치에 push하면 GitHub Actions([.github/workflows/deploy.yml](.github/workflows/deploy.yml))가
EC2에 SSH로 접속해 [deploy/deploy.sh](deploy/deploy.sh)를 실행한다.

- 서버: http://3.104.196.178 (nginx :80 → uvicorn 127.0.0.1:8000, systemd `fastapi-stock`)
- 필요한 GitHub Secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`
- 서버의 `~/fastapi-stock/.env`에 KIS 키가 있어야 한다 (git에 포함되지 않음).
- 주문/계좌 API(`/trading`)는 서버에서 비활성화되고 nginx에서도 403으로 막는다.

수동 배포: GitHub → Actions → Deploy to EC2 → Run workflow
