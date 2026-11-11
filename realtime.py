"""KIS 실시간(WebSocket) 시세 중계.

브라우저마다 KIS에 연결하지 않고, 서버가 KIS와 연결 1개만 유지하면서
받은 체결가/호가를 종목별 구독자(브라우저 WebSocket)들에게 나눠 준다.

- 체결가: H0STCNT0, 호가: H0STASP0
- 같은 종목을 여러 명이 봐도 KIS 구독은 1번 (참조 카운트)
- 연결이 끊기면 자동 재연결 후 다시 구독
"""

import asyncio
import json
import logging

import httpx
from websockets.asyncio.client import connect

import kis

log = logging.getLogger("realtime")

WS_URL = (
    "ws://ops.koreainvestment.com:31000"  # 모의투자
    if kis.BASE_URL == kis.MOCK_URL
    else "ws://ops.koreainvestment.com:21000"  # 실전투자
)
TR_TICK = "H0STCNT0"  # 국내주식 실시간 체결가
TR_BOOK = "H0STASP0"  # 국내주식 실시간 호가
QUEUE_SIZE = 200  # 브라우저가 느리면 오래된 메시지부터 버린다


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------- 메시지 정규화 (브라우저로 보내는 형식) ----------

def parse_tick(f: list[str]) -> dict:
    """H0STCNT0 한 건 → tick"""
    return {
        "type": "tick",
        "code": f[0],
        "time": f[1],  # HHMMSS
        "price": _int(f[2]),
        "sign": f[3],
        "change": _int(f[4]),
        "rate": _float(f[5]),
        "open": _int(f[7]),
        "high": _int(f[8]),
        "low": _int(f[9]),
        "tick_volume": _int(f[12]),  # 이번 체결량
        "volume": _int(f[13]),  # 누적 거래량
        "trade_value": _int(f[14]),  # 누적 거래대금
        "strength": _float(f[18]),  # 체결강도
    }


def parse_book(f: list[str]) -> dict:
    """H0STASP0 한 건 → book (10단계)"""
    return {
        "type": "book",
        "code": f[0],
        "time": f[1],
        "asks": [{"price": _int(f[3 + i]), "qty": _int(f[23 + i])} for i in range(10)],
        "bids": [{"price": _int(f[13 + i]), "qty": _int(f[33 + i])} for i in range(10)],
        "total_ask": _int(f[43]),
        "total_bid": _int(f[44]),
    }


def snapshot_tick(code: str) -> dict:
    """REST 현재가 → tick (장이 닫혀 있어도 첫 화면을 채우기 위해)"""
    o = kis.domestic_price(code)["output"]
    return {
        "type": "tick",
        "code": code,
        "time": None,
        "price": _int(o.get("stck_prpr")),
        "sign": o.get("prdy_vrss_sign"),
        "change": _int(o.get("prdy_vrss")),
        "rate": _float(o.get("prdy_ctrt")),
        "open": _int(o.get("stck_oprc")),
        "high": _int(o.get("stck_hgpr")),
        "low": _int(o.get("stck_lwpr")),
        "tick_volume": 0,
        "volume": _int(o.get("acml_vol")),
        "trade_value": _int(o.get("acml_tr_pbmn")),
        "strength": None,
        "snapshot": True,
    }


def snapshot_book(code: str) -> dict:
    """REST 호가 → book"""
    o = kis.domestic_orderbook(code)["output1"]
    return {
        "type": "book",
        "code": code,
        "time": None,
        "asks": [{"price": _int(o.get(f"askp{i}")), "qty": _int(o.get(f"askp_rsqn{i}"))} for i in range(1, 11)],
        "bids": [{"price": _int(o.get(f"bidp{i}")), "qty": _int(o.get(f"bidp_rsqn{i}"))} for i in range(1, 11)],
        "total_ask": _int(o.get("total_askp_rsqn")),
        "total_bid": _int(o.get("total_bidp_rsqn")),
        "snapshot": True,
    }


# ---------- KIS 연결 허브 ----------

class RealtimeHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}  # 종목코드 → 구독 브라우저 큐
        self._ws = None
        self._task: asyncio.Task | None = None
        self._approval_key: str | None = None
        self._lock = asyncio.Lock()

    # --- 브라우저 쪽 ---

    async def subscribe(self, code: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        async with self._lock:
            first = code not in self._subs
            self._subs.setdefault(code, set()).add(q)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
            elif first:
                await self._send_sub(code, True)
            if self._ws is not None:  # 이미 연결돼 있으면 새 구독자에게 상태를 바로 알린다
                q.put_nowait({"type": "status", "state": "live"})
        return q

    async def unsubscribe(self, code: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(code)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                del self._subs[code]
                await self._send_sub(code, False)

    def _publish(self, code: str, msg: dict) -> None:
        for q in list(self._subs.get(code, ())):
            if q.full():  # 느린 브라우저: 가장 오래된 메시지를 버리고 최신 값 유지
                q.get_nowait()
            q.put_nowait(msg)

    def _broadcast(self, msg: dict) -> None:
        for code in list(self._subs):
            self._publish(code, msg)

    # --- KIS 쪽 ---

    async def _get_approval_key(self) -> str:
        if self._approval_key:
            return self._approval_key
        if not kis.APP_KEY or not kis.APP_SECRET:
            raise kis.KISError(".env 파일에 KIS_APP_KEY, KIS_APP_SECRET을 설정하세요.")
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"{kis.BASE_URL}/oauth2/Approval",
                json={"grant_type": "client_credentials", "appkey": kis.APP_KEY, "secretkey": kis.APP_SECRET},
            )
        data = res.json()
        if "approval_key" not in data:
            raise kis.KISError(f"실시간 접속키 발급 실패: {data}")
        self._approval_key = data["approval_key"]
        return self._approval_key

    async def _send_sub(self, code: str, on: bool) -> None:
        if self._ws is None:
            return  # 연결되면 _run()이 현재 구독 목록 전체를 다시 등록한다
        for tr_id in (TR_TICK, TR_BOOK):
            msg = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1" if on else "2",  # 1 등록, 2 해제
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": tr_id, "tr_key": code}},
            }
            try:
                await self._ws.send(json.dumps(msg))
            except Exception:  # 끊긴 경우 재연결 때 다시 등록됨
                log.warning("구독 메시지 전송 실패: %s %s", tr_id, code)

    async def _run(self) -> None:
        backoff = 1
        while self._subs:
            try:
                key = await self._get_approval_key()
                async with connect(WS_URL, ping_interval=None, open_timeout=10) as ws:
                    backoff = 1
                    # 이 시점 이후 들어오는 구독은 subscribe()가 직접 등록하므로 중복 등록이 없다
                    async with self._lock:
                        self._ws = ws
                        codes = list(self._subs)
                    self._broadcast({"type": "status", "state": "live"})
                    for code in codes:
                        await self._send_sub(code, True)
                    async for raw in ws:
                        await self._handle(ws, raw, key)
                        if not self._subs:
                            break
            except asyncio.CancelledError:
                raise
            except kis.KISError as e:
                self._broadcast({"type": "status", "state": "error", "message": str(e)})
                await asyncio.sleep(30)
            except Exception as e:
                log.warning("KIS 실시간 연결 끊김: %r", e)
                self._broadcast({"type": "status", "state": "reconnecting"})
                self._approval_key = None if "approval" in str(e).lower() else self._approval_key
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self._ws = None

    async def _handle(self, ws, raw, key: str) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")

        # 실시간 데이터: "0|TR_ID|건수|필드^필드^..."  (첫 글자 1이면 암호화 → 체결통보 전용이라 사용 안 함)
        if raw[0] in "01":
            parts = raw.split("|", 3)
            if len(parts) < 4 or parts[0] != "0":
                return
            tr_id, count, body = parts[1], _int(parts[2]) or 1, parts[3].split("^")
            n = len(body) // count
            for i in range(count):
                f = body[i * n : (i + 1) * n]
                if tr_id == TR_TICK and len(f) > 18:
                    self._publish(f[0], parse_tick(f))
                elif tr_id == TR_BOOK and len(f) > 44:
                    self._publish(f[0], parse_book(f))
            return

        # 제어 메시지(JSON): 구독 응답, PINGPONG
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        header = msg.get("header", {})
        if header.get("tr_id") == "PINGPONG":
            await ws.send(raw)  # 받은 그대로 돌려줘야 연결이 유지된다
            return
        body = msg.get("body", {})
        if body.get("rt_cd") not in (None, "0") and "ALREADY" not in str(body.get("msg1", "")):
            code = header.get("tr_key")
            text = f"[{body.get('msg_cd')}] {body.get('msg1')}"
            log.warning("KIS 구독 오류 %s: %s", code, text)
            if code in self._subs:
                self._publish(code, {"type": "status", "state": "error", "message": text})


hub = RealtimeHub()
