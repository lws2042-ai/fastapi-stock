from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import kis

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
}


@app.get("/rankings/{kind}")
def rankings(kind: Literal["market-cap", "gainers", "volume"], limit: int = 10):
    """국내 순위 TOP N: market-cap(시가총액), gainers(급등), volume(거래량)"""
    return call(RANKINGS[kind], limit)


@app.get("/stocks/{code}")
def stock_price(code: str):
    """국내 주식 현재가 (예: /stocks/005930)"""
    return call(kis.domestic_price, code)


@app.get("/stocks/{code}/daily")
def stock_daily(code: str, period: Literal["D", "W", "M"] = "D"):
    """국내 주식 일/주/월별 시세 최근 30건"""
    return call(kis.domestic_daily, code, period)


@app.get("/stocks/{code}/orderbook")
def stock_orderbook(code: str):
    """국내 주식 호가 10단계"""
    return call(kis.domestic_orderbook, code)


@app.get("/overseas/{exchange}/{symbol}")
def overseas_price(exchange: str, symbol: str):
    """해외 주식 현재가 (예: /overseas/NAS/AAPL)"""
    return call(kis.overseas_price, exchange.upper(), symbol.upper())
