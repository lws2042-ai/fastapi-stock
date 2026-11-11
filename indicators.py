"""보조지표 계산과 매수/매도 크로스 신호 탐지.

모든 함수는 입력과 같은 길이의 리스트를 돌려주며, 계산할 수 없는 앞부분은 None.
"""

Series = list[float | None]


# ---------- 기본 ----------

def sma(values: Series, n: int) -> Series:
    out: Series = []
    for i in range(len(values)):
        w = values[max(0, i - n + 1) : i + 1]
        out.append(sum(w) / n if len(w) == n and None not in w else None)
    return out


def ema(values: Series, n: int) -> Series:
    """지수이동평균. 첫 값은 n개 단순평균으로 시작한다."""
    out: Series = [None] * len(values)
    k = 2 / (n + 1)
    prev, seed = None, []
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
        else:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


# ---------- 지표 ----------

def macd(closes: Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    sig = ema(line, signal)
    osc = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, osc


def rsi(closes: Series, n: int = 14) -> Series:
    """Wilder 방식 RSI"""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def calc(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[n] = calc(ag, al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = calc(ag, al)
    return out


def stochastic(highs, lows, closes, n: int = 14, k: int = 3, d: int = 3):
    """Slow 스토캐스틱 (14, 3, 3)"""
    fast_k: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        hh, ll = max(highs[i - n + 1 : i + 1]), min(lows[i - n + 1 : i + 1])
        fast_k[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    slow_k = sma(fast_k, k)
    slow_d = sma(slow_k, d)
    return slow_k, slow_d


# ---------- 크로스 탐지 ----------

def crosses(a: Series, b: Series | float):
    """a가 b를 상향 돌파하면 (i, 'up'), 하향 이탈하면 (i, 'down')"""
    if not isinstance(b, list):
        b = [b] * len(a)
    for i in range(1, len(a)):
        p_a, p_b, c_a, c_b = a[i - 1], b[i - 1], a[i], b[i]
        if None in (p_a, p_b, c_a, c_b):
            continue
        if p_a <= p_b and c_a > c_b:
            yield i, "up"
        elif p_a >= p_b and c_a < c_b:
            yield i, "down"


def _r(series: Series, nd: int = 2) -> Series:
    return [round(v, nd) if v is not None else None for v in series]


def analyze(candles: list[dict], display: int | None = None) -> dict:
    """캔들(오래된 순)로 지표와 신호를 계산한다.

    display: 앞부분은 지표 계산용 워밍업으로만 쓰고 마지막 display개만 돌려준다.
    """
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    macd_line, macd_sig, macd_osc = macd(closes)
    rsi14 = rsi(closes)
    stoch_k, stoch_d = stochastic(highs, lows, closes)
    ma3, ma20, ma60 = sma(closes, 3), sma(closes, 20), sma(closes, 60)

    ind = {
        "ma3": ma3, "ma20": ma20, "ma60": ma60,
        "macd": macd_line, "macd_signal": macd_sig, "macd_osc": macd_osc,
        "rsi": rsi14,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
    }

    signals = []

    def add(i, kind, indicator, label):
        signals.append({"index": i, "date": candles[i]["date"], "type": kind,
                        "indicator": indicator, "label": label, "price": closes[i]})

    for i, d in crosses(macd_line, macd_sig):
        add(i, "buy" if d == "up" else "sell", "MACD",
            "MACD 골든크로스 (시그널 상향 돌파)" if d == "up" else "MACD 데드크로스 (시그널 하향 이탈)")

    for i, d in crosses(rsi14, 30):
        if d == "up":
            add(i, "buy", "RSI", "RSI 30 상향 돌파 (과매도 탈출)")
    for i, d in crosses(rsi14, 70):
        if d == "down":
            add(i, "sell", "RSI", "RSI 70 하향 이탈 (과매수 이탈)")

    for i, d in crosses(stoch_k, stoch_d):
        if d == "up" and stoch_d[i] < 20:
            add(i, "buy", "STOCH", "스토캐스틱 침체권(20↓) 골든크로스")
        elif d == "down" and stoch_d[i] > 80:
            add(i, "sell", "STOCH", "스토캐스틱 과열권(80↑) 데드크로스")

    for i, d in crosses(ma3, ma20):
        add(i, "buy" if d == "up" else "sell", "MA",
            "이평 골든크로스 (3일선 > 20일선)" if d == "up" else "이평 데드크로스 (3일선 < 20일선)")

    start = max(0, len(candles) - display) if display else 0
    signals = sorted((s for s in signals if s["index"] >= start), key=lambda s: s["index"])
    for s in signals:
        s["index"] -= start

    return {
        "candles": candles[start:],
        "indicators": {k: _r(v[start:]) for k, v in ind.items()},
        "signals": signals,
    }
