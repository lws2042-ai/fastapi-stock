from pathlib import Path
from typing import Literal

import asyncio
import os

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import indicators
import kis
import trading
from realtime import hub, snapshot_book, snapshot_tick

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Stock API (한국투자증권 KIS)")


def call(fn, *args):
    try:
        return fn(*args)
    except kis.KISError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/", include_in_schema=False)
def read_home():
    """주식 시세 화면"""
    return FileResponse(STATIC_DIR / "index.html")


RANKINGS = {
    "market-cap": kis.rank_market_cap,
    "gainers": kis.rank_gainers,
    "volume": kis.rank_volume,
    "trade-value": kis.rank_trade_value,
}


@app.get("/rankings/{kind}")
def rankings(
    kind: Literal["market-cap", "gainers", "volume", "trade-value"],
    market: Literal["kospi", "kosdaq", "all"] = "kospi",
    limit: int = Query(10, ge=1, le=30),
):
    """국내 순위 TOP N: market-cap(시가총액) / gainers(급등) / volume(거래량) / trade-value(거래대금)"""
    return call(RANKINGS[kind], limit, market)


@app.get("/stocks/{code}")
def stock_price(code: str):
    """국내 주식 현재가 (예: /stocks/005930)"""
    return call(kis.domestic_price, code)


@app.get("/stocks/{code}/daily")
def stock_daily(code: str, period: Literal["D", "W", "M"] = "D"):
    """국내 주식 일/주/월별 시세 최근 30건"""
    return call(kis.domestic_daily, code, period)


WARMUP = 60  # 지표 계산용으로 더 받아오는 봉 수 (MA60, MACD 26+9)


@app.get("/stocks/{code}/chart")
def stock_chart(
    code: str,
    period: Literal["D", "W", "M", "60"] = "D",
    count: int = Query(120, ge=20, le=500),
):
    """캔들 + 보조지표(MA, MACD, RSI, 스토캐스틱) + 매수/매도 크로스 신호

    period: D(일봉) / W(주봉) / M(월봉) / 60(60분봉, 최근 12거래일)
    """
    if period == "60":
        candles = call(kis.domestic_hourly, code)
        # 앞쪽 26봉은 MACD 계산 워밍업으로만 쓴다
        return indicators.analyze(candles, display=max(len(candles) - 26, 1))
    candles = call(kis.domestic_chart, code, period, count + WARMUP)
    return indicators.analyze(candles, display=count)


@app.get("/stocks/{code}/orderbook")
def stock_orderbook(code: str):
    """국내 주식 호가 10단계"""
    return call(kis.domestic_orderbook, code)


@app.websocket("/ws/stocks/{code}")
async def stock_realtime(ws: WebSocket, code: str):
    """국내 주식 실시간 체결가(tick) + 호가(book) 스트림.

    접속 직후 REST 현재가/호가를 snapshot으로 한 번 보내고(장 마감 중에도 화면이 채워지도록),
    이후 KIS 실시간 데이터를 중계한다. 메시지 type: tick / book / status
    """
    await ws.accept()
    if not (len(code) == 6 and code.isalnum()):
        await ws.close(code=1008, reason="invalid code")
        return

    q = await hub.subscribe(code)
    # 브라우저가 연결을 끊으면 receive_text가 WebSocketDisconnect를 던지므로 함께 감시한다
    receiver = asyncio.create_task(ws.receive_text())
    getter = None
    try:
        for fn in (snapshot_tick, snapshot_book):
            try:
                await ws.send_json(await asyncio.to_thread(fn, code))
            except kis.KISError as e:
                await ws.send_json({"type": "status", "state": "error", "message": str(e)})

        while True:
            getter = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                receiver.result()  # 끊김이면 여기서 예외
                receiver = asyncio.create_task(ws.receive_text())  # 브라우저가 보낸 메시지는 무시
            if getter in done:
                await ws.send_json(getter.result())
            else:
                getter.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        if getter:
            getter.cancel()
        await hub.unsubscribe(code, q)


# ---------- 주문 / 계좌 (이 PC에서 온 요청만 허용) ----------

# 서버 배포 시에는 ENABLE_TRADING_API=false 로 주문/계좌 API 자체를 끈다.
# (nginx 뒤에서는 모든 요청이 127.0.0.1에서 온 것처럼 보이므로 local_only만으로는 막을 수 없다)
TRADING_API = os.getenv("ENABLE_TRADING_API", "true").lower() == "true"


def local_only(request: Request):
    """계좌·주문 API는 localhost 요청만 받는다. 외부 배포 전에는 반드시 로그인 인증을 추가할 것."""
    if not TRADING_API:
        raise HTTPException(status_code=404, detail="주문/계좌 API가 꺼져 있습니다.")
    if request.client is None or request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="주문/계좌 API는 이 PC에서만 사용할 수 있습니다.")


class OrderIn(BaseModel):
    code: str = Field(pattern=r"^[0-9A-Z]{6}$")
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)
    price: int | None = Field(default=None, gt=0, description="없으면 시장가")


@app.get("/trading/config", dependencies=[Depends(local_only)])
def trading_config():
    """주문 설정: 실제 주문 여부(enabled), 모의계좌 여부, 1회 한도"""
    return trading.config()


@app.post("/trading/orders", dependencies=[Depends(local_only)])
def place_order(o: OrderIn):
    """현금 매수/매도. KIS_ORDER_ENABLED=true 가 아니면 모의 실행(전송 안 함)"""
    try:
        return trading.place_order(o.code, o.side, o.qty, o.price)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except kis.KISError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/trading/orders", dependencies=[Depends(local_only)])
def order_log(limit: int = Query(50, ge=1, le=500)):
    """주문 요청 기록 (최신순)"""
    return trading.order_log(limit)


@app.get("/trading/balance", dependencies=[Depends(local_only)])
def balance():
    """보유 종목 + 예수금 (조회 전용)"""
    return call(trading.balance)


@app.get("/overseas/{exchange}/{symbol}")
def overseas_price(exchange: str, symbol: str):
    """해외 주식 현재가 (예: /overseas/NAS/AAPL)"""
    return call(kis.overseas_price, exchange.upper(), symbol.upper())
