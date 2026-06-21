#!/usr/bin/env python3
"""S&P500 ウォッチャー"""

import warnings
warnings.filterwarnings("ignore")

import json
import html as html_mod
import os
import re
import socket
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Dict, List, Tuple

import pytz
import yfinance as yf

NYSE_TZ = pytz.timezone("America/New_York")
JST     = pytz.timezone("Asia/Tokyo")

# アプリのバージョン（変更履歴は CHANGELOG.md を参照）
APP_VERSION = "2026.06.21"

# 株価指数
INDICES: List[Tuple[str, str]] = [
    ("S&P 500",        "^GSPC"),
    ("S&P500（先物）", "ES=F"),
    ("NASDAQ 100",      "^NDX"),
    ("日経平均株価",    "^N225"),
    ("オルカン",       "2559.T"),
]

# 為替ペア
FX_PAIRS: List[Tuple[str, str]] = [
    ("ドル円",   "JPY=X"),
    ("ユーロ円", "EURJPY=X"),
]

ALL_CARDS = INDICES + FX_PAIRS

# yfinance が株式分割を反映するまでの一時補正テーブル
#   { symbol: 分割比率 }  例: 1→10 分割なら 10
# yfinance 側で分割調整が反映されると価格の段差が消え、補正は自動的に無効になる。
# 反映後はこの行を削除して問題ない（残しても二重補正は起きない）。
SPLIT_OVERRIDES: Dict[str, float] = {
    "2559.T": 10.0,  # オルカン(MAXIS全世界株式) 2026-06-05 に 1→10 分割
}

# ポートフォリオデータ保存先
def _portfolio_file() -> str:
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "portfolio.json")

PERIODS: List[Tuple[str, str, str]] = [
    ("1日",   "1d",  "5m"),
    ("1週",   "5d",  "1h"),
    ("1ヶ月", "1mo", "1d"),
    ("3ヶ月", "3mo", "1d"),
    ("6ヶ月", "6mo", "1d"),
    ("年初来", "ytd", "1d"),
    ("1年",   "1y",  "1wk"),
    ("5年",   "5y",  "1mo"),
    ("全期間", "max", "resample_qtr"),
]

NEWS_QUERIES = [
    "S&P500 OR 日経平均 株価 急落 OR 急騰 OR 相場",
    "FRB OR FOMC 金利 OR 利上げ OR 利下げ",
    "米国 雇用統計 OR CPI OR 物価指数 OR GDP",
    "円安 OR 円高 OR 為替介入",
    "株価 暴落 OR 急落 OR 急騰 OR 最高値",
    "ウクライナ OR イスラエル OR イラン OR 中東 経済 OR 制裁",
    "トランプ 関税 OR 貿易 OR 経済",
    "日銀 金融政策 OR 利上げ OR 為替",
    "リセッション OR 景気後退 OR 原油 価格",
    "NASDAQ OR ダウ 株価",
]

POSITIVE_KW = [
    "株価", "相場", "急落", "急騰", "暴落", "下落", "上昇", "最高値", "最安値",
    "S&P", "Ｓ＆Ｐ", "ナスダック", "NASDAQ", "日経", "ダウ",
    "雇用統計", "CPI", "物価", "GDP", "失業率", "インフレ", "デフレ",
    "金利", "利上げ", "利下げ", "FOMC", "FRB", "日銀", "金融政策", "利率",
    "円安", "円高", "ドル高", "ドル安", "為替", "介入",
    "ウクライナ", "イスラエル", "イラン", "中東", "ロシア",
    "原油", "関税", "制裁", "貿易", "景気", "リセッション", "景気後退",
    "トランプ", "経済指標", "連邦", "財務",
]

NEGATIVE_KW = [
    "基準価格", "投資信託情報", "積み立て", "おすすめ", "ランキング",
    "口座開設", "手数料", "初心者", "始め方", "選び方", "入門",
    "キャンペーン", "セミナー", "無料", "特典", "比較", "解説",
]


def score_item(title: str) -> int:
    score = 0
    for kw in POSITIVE_KW:
        if kw in title:
            score += 1
    for kw in NEGATIVE_KW:
        if kw in title:
            score -= 2
    return score


def symbol_market_open(symbol: str) -> bool:
    """各シンボルの市場が現在開いているか判定する。"""
    now_et  = datetime.now(NYSE_TZ)
    now_jst = datetime.now(JST)

    if symbol in ("^GSPC", "ES=F", "^NDX"):
        # NYSE: 平日 9:30–16:00 ET
        if now_et.weekday() >= 5:
            return False
        open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_t <= now_et < close_t

    elif symbol in ("^N225", "2559.T"):
        # 東証: 平日 9:00–11:30 / 12:30–15:30 JST
        if now_jst.weekday() >= 5:
            return False
        t        = now_jst
        am_open  = t.replace(hour=9,  minute=0,  second=0, microsecond=0)
        am_close = t.replace(hour=11, minute=30, second=0, microsecond=0)
        pm_open  = t.replace(hour=12, minute=30, second=0, microsecond=0)
        pm_close = t.replace(hour=15, minute=30, second=0, microsecond=0)
        return (am_open <= t < am_close) or (pm_open <= t < pm_close)

    elif symbol in ("JPY=X", "EURJPY=X"):
        # FX: 月曜7:00 JST – 土曜7:00 JST
        wd = now_jst.weekday()
        if wd == 6:  # 日曜
            return False
        if wd == 5:  # 土曜
            return now_jst < now_jst.replace(hour=7, minute=0, second=0, microsecond=0)
        if wd == 0:  # 月曜
            return now_jst >= now_jst.replace(hour=7, minute=0, second=0, microsecond=0)
        return True  # 火〜金

    return False


def market_status() -> Tuple[bool, str]:
    now = datetime.now(NYSE_TZ)
    if now.weekday() >= 5:
        return False, "週末クローズ"
    pre    = now.replace(hour=4,  minute=0,  second=0, microsecond=0)
    open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    ah     = now.replace(hour=20, minute=0,  second=0, microsecond=0)
    if open_ <= now < close_:
        return True,  "市場開場中 (NYSE)"
    elif pre <= now < open_:
        return False, "プレマーケット"
    elif close_ <= now < ah:
        return False, "アフターアワーズ"
    else:
        return False, "市場クローズ"


def _unsplit_adjust(symbol: str, values) -> List[float]:
    """yfinance の分割過渡期データを整える。
    (1) 前後の終値と桁が違う単発の異常値（1/10 の誤ティック等）を近傍平均で補間。
    (2) 未反映の株式分割で生じた段差（約 ratio 倍）を末尾から遡って検出し、
        それより前を 1/ratio に補正する。
    yfinance 側で調整が反映されると段差が消えるため、本処理は自動的に無効化される。"""
    out = [float(v) for v in values]
    n = len(out)
    if n < 3:
        return out
    # (1) 現在の水準（直近5点の中央値＝単発外れ値に頑健）を基準に、各点を現在スケールへ正規化。
    #     分割前の約 ratio 倍の点は 1/ratio、1/ratio の誤ティックは ratio 倍に揃える。
    #     段差の位置検出に頼らないため、混在データや単発異常値があっても破綻しない。
    ratio = SPLIT_OVERRIDES.get(symbol)
    if ratio and ratio > 1:
        tail = sorted(v for v in out[-5:] if v > 0)
        if tail:
            level = tail[len(tail) // 2]
            hi = level * ratio * 0.6      # これ超 → 分割前(約ratio倍)
            lo = level / (ratio * 0.6)    # これ未満 → 1/ratio の異常値
            for i in range(n):
                if out[i] <= 0:
                    continue
                if out[i] > hi:
                    out[i] /= ratio
                elif out[i] < lo:
                    out[i] *= ratio
    # (2) 残った単発の異常値を近傍平均で平滑化（前後平均と 3 倍以上乖離する単点のみ）。
    #     持続的な水準変化は隣接点も同水準のため対象にならない。
    for i in range(1, n - 1):
        a, b, c = out[i - 1], out[i], out[i + 1]
        if a > 0 and b > 0 and c > 0:
            nb = (a + c) / 2
            if nb > 0 and (b / nb > 3 or nb / b > 3):
                out[i] = nb
    return out


def fetch_quote(symbol: str) -> Optional[Dict]:
    try:
        t  = yf.Ticker(symbol)
        fi = t.fast_info
        price = fi.last_price

        # 週末跨ぎ・分割直後の混在データに備えて 10 営業日分取得し、
        # (日付, 終値) の並びを作る
        hist = t.history(period="10d", interval="1d").dropna(subset=["Close"])
        rows = [(idx.date(), float(c)) for idx, c in zip(hist.index, hist["Close"])]

        if price is None:
            if not rows:
                return None
            price = rows[-1][1]
        price = float(price)
        if price <= 0:
            return None

        # 前日終値: 最新日より前で、当日価格と同じ桁（1/3〜3倍）に収まる最新の終値を採用。
        # 株式分割を yfinance が未調整の間は古い終値が約10倍ズレたり、単発の異常値
        # （1/10 の誤ティック等）が混ざるため、桁が合わない値は飛ばして選ぶ。
        def _in_band(c: float) -> bool:
            return c > 0 and price / 3 <= c <= price * 3

        prev_close = None
        last_date = rows[-1][0] if rows else None
        for d, c in reversed(rows):
            if last_date is not None and d < last_date and _in_band(c):
                prev_close = c
                break
        if prev_close is None:
            pc = fi.previous_close
            prev_close = float(pc) if pc and _in_band(float(pc)) else None
        if prev_close is None or prev_close == 0:
            return None

        chg = price - prev_close
        pct = chg / prev_close * 100
        return {"price": price, "change": chg, "pct": pct}
    except Exception:
        return None


def fetch_history(symbol: str, period: str, interval: str) -> Dict:
    try:
        today_start_index = None
        if interval == "resample_qtr":
            df = yf.Ticker(symbol).history(period="max", interval="1d")
            if df.empty:
                return {"labels": [], "values": [], "today_start_index": None}
            close = df["Close"].resample("QE").last().dropna()
            labels = [str(t.date()) for t in close.index]
        elif period == "1d":
            # 過去24時間相当: 2日分の5分足を取得
            df = yf.Ticker(symbol).history(period="2d", interval="5m")
            if df.empty:
                return {"labels": [], "values": [], "today_start_index": None}
            close = df["Close"].dropna()
            # ラベルは "YYYY-MM-DD HH:MM" 形式（日付をまたぐため一意にする）
            labels = [t.strftime("%Y-%m-%d %H:%M") for t in close.index]
            # 今日のセッション開始インデックスを検出（最後のデータ点と同じ日付）
            if len(close) > 0:
                last_date = close.index[-1].date()
                for i, t in enumerate(close.index):
                    if t.date() == last_date:
                        today_start_index = i
                        break
        else:
            df = yf.Ticker(symbol).history(period=period, interval=interval)
            if df.empty:
                return {"labels": [], "values": [], "today_start_index": None}
            close = df["Close"].dropna()
            labels = [str(t.date()) for t in close.index]
        values = [round(v, 2) for v in _unsplit_adjust(symbol, close.values)]
        return {"labels": labels, "values": values, "today_start_index": today_start_index}
    except Exception:
        return {"labels": [], "values": [], "today_start_index": None}


def fetch_news_items() -> List[str]:
    scored: List[Tuple[int, str]] = []
    seen = set()
    for q in NEWS_QUERIES:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                root = ET.fromstring(r.read())
            for item in root.findall(".//item")[:5]:
                title = item.findtext("title", "").split(" - ")[0].strip()
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", title))
                if title and title not in seen:
                    seen.add(title)
                    scored.append((score_item(title), title))
        except Exception:
            pass
    scored.sort(key=lambda x: x[0], reverse=True)
    # スコア閾値: ≥2 を優先、なければ ≥1、それもなければ全件
    for threshold in (2, 1, 0):
        filtered = [t for s, t in scored if s >= threshold]
        if filtered:
            return filtered[:20]
    return [t for _, t in scored[:20]]


def fetch_period_start_price(symbol: str, period: str, interval: str) -> Optional[float]:
    """期間開始の終値を返す。fetch_history と同じ period= クエリを使い、カードとチャートの % を一致させる。"""
    try:
        if interval == "5m":          # 1日は quote の prev_close を使うため不要
            return None
        if interval == "resample_qtr":
            df = yf.Ticker(symbol).history(period="max", interval="3mo")
            if df.empty:
                return None
            return float(df["Close"].dropna().iloc[0])
        df = yf.Ticker(symbol).history(period=period, interval="1d")
        if df.empty:
            return None
        close = df["Close"].dropna()
        return float(close.iloc[0]) if len(close) > 0 else None
    except Exception:
        return None


def fetch_origin_close(symbol: str, since_date: str) -> Optional[float]:
    """since_date 以降で最初の終値（分割補正済み）を返す。
    ポートフォリオの「入力時起点の累積騰落」の基準値に使う。
    現在価格(fetch_quote)も分割補正済みなので、両端が同一基準で比較できる。"""
    try:
        df = yf.Ticker(symbol).history(start=since_date, interval="1d")
        if df.empty:
            return None
        close = df["Close"].dropna()
        if len(close) == 0:
            return None
        vals = _unsplit_adjust(symbol, close.values)
        return float(vals[0]) if vals and vals[0] else None
    except Exception:
        return None


# ── Python API ────────────────────────────────────────────────────────────────
class Api:
    def fetch_all(self, period_idx: int, chart_symbol: str = "^GSPC") -> str:
        period_idx = int(period_idx)
        _, period_arg, interval = PERIODS[period_idx]

        def get_card_data(name_sym: Tuple[str, str]):
            name, sym = name_sym
            q     = fetch_quote(sym)
            start = fetch_period_start_price(sym, period_arg, interval)
            return sym, name, q, start

        with ThreadPoolExecutor(max_workers=len(ALL_CARDS)) as ex:
            results = list(ex.map(get_card_data, ALL_CARDS))

        quotes = {}
        period_changes = {}
        for sym, name, q, start in results:
            quotes[sym] = {
                "name":   name,
                "symbol": sym,
                "price":  round(q["price"], 2)  if q else None,
                "change": round(q["change"], 2) if q else None,
                "pct":    round(q["pct"], 2)    if q else None,
            }
            price = q["price"] if q else None
            if price and start and start != 0:
                period_changes[sym] = round((price / start - 1) * 100, 2)
            else:
                period_changes[sym] = None

        hist = fetch_history(str(chart_symbol), period_arg, interval)
        is_open, status = market_status()
        symbols_open = {sym: symbol_market_open(sym) for _, sym in ALL_CARDS}
        now_jst = datetime.now(JST).strftime("%m/%d %H:%M")

        return json.dumps({
            "quotes":          quotes,
            "period_changes":  period_changes,
            "history":         hist,
            "is_open":         is_open,
            "symbols_open":    symbols_open,
            "status":          status,
            "updated":         now_jst,
        })

    def fetch_news(self) -> str:
        return json.dumps(fetch_news_items())

    def load_portfolio(self) -> str:
        try:
            path = _portfolio_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return json.dumps({"holdings": []})

    def save_portfolio(self, data: str) -> str:
        try:
            parsed = json.loads(data)
            with open(_portfolio_file(), "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def origin_closes(self, data: str) -> str:
        """[{key,symbol,since}] のベンチマーク origin 終値を {key: close} で返す。
        同一 (symbol, since) はキャッシュして yfinance 呼び出しを重複させない。"""
        try:
            queries = json.loads(data).get("queries", [])
        except Exception:
            return json.dumps({})
        cache: Dict[Tuple[str, str], Optional[float]] = {}
        out: Dict[str, Optional[float]] = {}
        for q in queries:
            key, sym, since = q.get("key"), q.get("symbol"), q.get("since")
            if not key or not sym or not since:
                continue
            ck = (sym, since)
            if ck not in cache:
                cache[ck] = fetch_origin_close(sym, since)
            out[key] = cache[ck]
        return json.dumps(out)


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>S&P500 ウォッチャー</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ヘッダー */
#header{background:#21262d;padding:10px 18px;display:flex;align-items:center;gap:16px;flex-shrink:0;border-bottom:1px solid #30363d}
#title{font-size:17px;font-weight:700;color:#c9d1d9;white-space:nowrap}
#app-ver{font-size:10px;font-weight:400;color:#484f58;vertical-align:middle}
#market-status{font-size:12px;color:#8b949e;margin-right:auto}
#updated{font-size:11px;color:#6e7681}
#refresh-btn{background:#58a6ff;color:#fff;border:none;padding:6px 16px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}
#refresh-btn:hover{background:#388bfd}
#refresh-btn:disabled{background:#30363d;color:#6e7681;cursor:default}

/* カード行 */
#cards{display:flex;gap:6px;padding:8px 12px;flex-shrink:0;align-items:stretch}
.card{flex:1;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:9px 12px;min-width:0;cursor:pointer;transition:border-color .15s,background .15s;position:relative}
.card:hover{background:#1c2128}
.card.selected{border-color:var(--card-color,#58a6ff);background:#1c2128;box-shadow:0 0 0 1px var(--card-color,#58a6ff) inset}
.card-name{font-size:10px;color:#8b949e;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-price{font-size:18px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-change{font-size:10px;margin-top:2px;white-space:nowrap}
.card-period{font-size:10px;margin-top:3px;color:#6e7681;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.up{color:#f85149}.dn{color:#3fb950}.na{color:#8b949e}
.card-closed{opacity:0.45;transition:opacity .3s}
.card-sep{width:1px;background:#30363d;flex-shrink:0;margin:4px 2px}

/* チャートエリア */
#chart-area{flex:1;background:#161b22;margin:0 12px 6px;border:1px solid #30363d;border-radius:8px;display:flex;flex-direction:column;min-height:0}
#period-bar{padding:7px 12px;display:flex;align-items:center;gap:5px;flex-shrink:0;border-bottom:1px solid #30363d;flex-wrap:wrap}
#period-bar span{font-size:11px;color:#8b949e;margin-right:3px}
.period-btn{background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:2px 9px;font-size:11px;cursor:pointer;transition:all .15s}
.period-btn:hover{border-color:#8b949e;color:#c9d1d9}
.period-btn.active{background:#58a6ff;color:#fff;border-color:#58a6ff}
#chart-title{font-size:12px;font-weight:700;white-space:nowrap}
#chart-wrap{flex:1;position:relative;padding:8px 8px 4px;min-height:0}
#myChart{width:100%!important;height:100%!important}

/* ニューステッカー */
#news-bar{background:#0d1117;border-top:1px solid #30363d;height:30px;display:flex;align-items:center;overflow:hidden;flex-shrink:0}
.news-label{background:#58a6ff;color:#fff;padding:0 10px;font-size:10px;font-weight:700;white-space:nowrap;height:100%;display:flex;align-items:center;flex-shrink:0;letter-spacing:.05em}
#ticker-container{flex:1;overflow:hidden;height:100%;display:flex;align-items:center;position:relative}
#ticker-track{display:inline-flex;align-items:center;white-space:nowrap;animation:ticker-scroll 80s linear infinite;will-change:transform}
#ticker-track:hover{animation-play-state:paused}
.ticker-item{font-size:11px;color:#c9d1d9;padding:0 16px;cursor:default}
.ticker-dot{color:#30363d;font-size:8px}
@keyframes ticker-scroll{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.new-badge{position:absolute;top:7px;right:8px;font-size:9px;font-weight:700;color:#fff;background:#f0b429;border-radius:3px;padding:1px 5px;animation:badge-fade 30s ease forwards;pointer-events:none;z-index:2}
@keyframes badge-fade{0%,80%{opacity:1}100%{opacity:0}}
.close-badge{position:absolute;top:7px;right:8px;font-size:9px;font-weight:600;color:#6e7681;background:#161b22;border:1px solid #30363d;border-radius:3px;padding:1px 5px;pointer-events:none;z-index:1}

/* スピナー */
#spinner{display:none;position:fixed;inset:0;background:rgba(13,17,23,.7);align-items:center;justify-content:center;z-index:99}
#spinner.show{display:flex}
.spin{width:34px;height:34px;border:3px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* PFボタン */
#pf-toggle-btn{background:transparent;border:1px solid #30363d;color:#8b949e;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s}
#pf-toggle-btn:hover{border-color:#8b949e;color:#c9d1d9}
#pf-toggle-btn.active{border-color:#58a6ff;color:#58a6ff;background:#1c2128}

/* ポートフォリオパネル */
#pf-overlay{display:none;position:fixed;inset:0;background:rgba(13,17,23,.55);z-index:50}
#pf-overlay.show{display:block}
#pf-panel{position:fixed;top:0;right:0;bottom:0;width:340px;background:#161b22;border-left:1px solid #30363d;display:none;flex-direction:column;z-index:51;box-shadow:-6px 0 24px rgba(0,0,0,.5)}
#pf-panel.show{display:flex}
#pf-hdr{padding:10px 14px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #30363d;flex-shrink:0}
#pf-hdr-title{font-size:13px;font-weight:700;flex:1;color:#c9d1d9}
.pf-btn{background:transparent;border:1px solid #30363d;color:#8b949e;padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;transition:all .15s}
.pf-btn:hover{border-color:#8b949e;color:#c9d1d9}
#pf-body{flex:1;overflow-y:auto;padding:10px 14px}
.pf-row{padding:9px 0;border-bottom:1px solid #21262d}
.pf-row:last-child{border-bottom:none}
.pf-row-name{font-size:11px;color:#8b949e;margin-bottom:4px}
.pf-row-vals{display:flex;align-items:baseline;gap:7px}
.pf-row-amt{font-size:16px;font-weight:700;color:#c9d1d9}
.pf-row-chg{font-size:11px}
.pf-row-prev{font-size:10px;color:#484f58;margin-top:3px}
.pf-row-origin{font-size:10px;color:#6e7681;margin-top:2px}
#pf-foot{padding:12px 14px;border-top:1px solid #30363d;background:#21262d;flex-shrink:0}
.pf-foot-label{font-size:10px;color:#8b949e;margin-bottom:3px}
.pf-foot-total{font-size:19px;font-weight:700;color:#c9d1d9}
.pf-foot-chg{font-size:12px;margin-top:3px}
.pf-foot-note{font-size:9px;color:#484f58;margin-top:5px}
.pf-inp{width:100%;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:5px 8px;border-radius:4px;font-size:13px;margin-top:4px;outline:none}
.pf-inp:focus{border-color:#58a6ff}
.pf-sel{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:5px 8px;border-radius:4px;font-size:12px;outline:none;cursor:pointer}
.pf-sel:focus{border-color:#58a6ff}
.pf-add-area{margin-top:10px;padding-top:10px;border-top:1px solid #30363d}
.pf-add-row{display:flex;flex-direction:column;gap:5px}
.pf-add-label{font-size:10px;color:#8b949e}
.pf-add-actions{display:flex;gap:6px;margin-top:6px}
.pf-del-btn{background:transparent;border:1px solid #f85149;color:#f85149;padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer;transition:all .15s;margin-left:auto;display:block}
.pf-del-btn:hover{background:#f8514920}
</style>
</head>
<body>

<div id="header">
  <div id="title">S&P500 ウォッチャー <span id="app-ver">v__APP_VERSION__</span></div>
  <div id="market-status">読込中...</div>
  <div id="updated"></div>
  <button id="pf-toggle-btn" onclick="togglePortfolio()" title="ポートフォリオ速報">📊 PF</button>
  <button id="refresh-btn" onclick="doRefresh()">↻ 更新</button>
</div>

<div id="cards">
  <div class="card selected" id="card-GSPC" style="--card-color:#58a6ff" onclick="selectCard('^GSPC',this,'S&amp;P 500')">
    <div class="card-name">S&amp;P 500</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card" id="card-ES" style="--card-color:#e3b341" onclick="selectCard('ES=F',this,'S&amp;P500（先物）')">
    <div class="card-name">S&amp;P500（先物）</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card" id="card-IXIC" style="--card-color:#a371f7" onclick="selectCard('^NDX',this,'NASDAQ 100')">
    <div class="card-name">NASDAQ 100</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card" id="card-N225" style="--card-color:#ff7b72" onclick="selectCard('^N225',this,'日経平均株価')">
    <div class="card-name">日経平均株価</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card" id="card-OLCAN" style="--card-color:#39d353" onclick="selectCard('2559.T',this,'オルカン')">
    <div class="card-name">オルカン</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card-sep"></div>
  <div class="card" id="card-USDJPY" style="--card-color:#f0b429" onclick="selectCard('JPY=X',this,'ドル円')">
    <div class="card-name">ドル円  (USD/JPY)</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
  <div class="card" id="card-EURJPY" style="--card-color:#e879f9" onclick="selectCard('EURJPY=X',this,'ユーロ円')">
    <div class="card-name">ユーロ円  (EUR/JPY)</div>
    <div class="card-price na">---</div>
    <div class="card-change na">　</div>
    <div class="card-period"></div>
  </div>
</div>

<div id="chart-area">
  <div id="period-bar">
    <span>期間:</span>
    <button class="period-btn active" onclick="changePeriod(0)">1日</button>
    <button class="period-btn" onclick="changePeriod(1)">1週</button>
    <button class="period-btn" onclick="changePeriod(2)">1ヶ月</button>
    <button class="period-btn" onclick="changePeriod(3)">3ヶ月</button>
    <button class="period-btn" onclick="changePeriod(4)">6ヶ月</button>
    <button class="period-btn" onclick="changePeriod(5)">年初来</button>
    <button class="period-btn" onclick="changePeriod(6)">1年</button>
    <button class="period-btn" onclick="changePeriod(7)">5年</button>
    <button class="period-btn" onclick="changePeriod(8)">全期間</button>
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <button id="log-btn" class="period-btn" onclick="toggleLogScale()" title="対数スケール切り替え">対数</button>
      <div id="chart-title"></div>
    </div>
  </div>
  <div id="chart-wrap">
    <canvas id="myChart"></canvas>
  </div>
</div>

<div id="news-bar">
  <div class="news-label">MARKET NEWS</div>
  <div id="ticker-container">
    <div id="ticker-track"><span class="ticker-item" style="color:#8b949e">ニュース取得中...</span></div>
  </div>
</div>

<div id="spinner"><div class="spin"></div></div>

<!-- ポートフォリオパネル -->
<div id="pf-overlay" onclick="togglePortfolio()"></div>
<div id="pf-panel">
  <div id="pf-hdr">
    <div id="pf-hdr-title">📊 ポートフォリオ速報</div>
    <button class="pf-btn" id="pf-edit-btn" onclick="togglePfEdit()">編集</button>
    <button class="pf-btn" onclick="togglePortfolio()">✕</button>
  </div>
  <div id="pf-body"><div style="color:#6e7681;font-size:12px;padding:24px;text-align:center">読込中...</div></div>
  <div id="pf-foot" style="display:none">
    <div class="pf-foot-label">合計評価額（推定）</div>
    <div class="pf-foot-total" id="pf-foot-total"></div>
    <div class="pf-foot-chg" id="pf-foot-chg"></div>
    <div class="pf-foot-note" id="pf-foot-note">※ 入力時からの指数・為替変動で再計算した速報値</div>
  </div>
</div>

<script>
// ── 定数 ──────────────────────────────────────────────────────────────────────
const CARD_SYMS   = ['^GSPC','ES=F','^NDX','^N225','2559.T','JPY=X','EURJPY=X'];
const CARD_IDS    = ['GSPC','ES','IXIC','N225','OLCAN','USDJPY','EURJPY'];
const CARD_NAMES  = ['S&P 500','S&P500（先物）','NASDAQ 100','日経平均株価','オルカン','ドル円','ユーロ円'];
const COLORS      = ['#58a6ff','#e3b341','#a371f7','#ff7b72','#39d353','#f0b429','#e879f9'];
const FX_SYMS     = new Set(['JPY=X','EURJPY=X']);
const PERIOD_NAMES = ['1日','1週','1ヶ月','3ヶ月','6ヶ月','年初来','1年','5年','全期間'];

let currentPeriod = 0;
let chartSymbol   = '^GSPC';
let chartSymName  = 'S&P 500';
let chart         = null;
let autoTimer     = null;
let newsTimer     = null;
let prevQuotes    = {};
let logScale      = false;
let lastChartArgs = null;  // [hist, symQ, futuresQ]
let latestQuotes  = null;
let pfOpen        = false;
let pfEdit        = false;
let pfHoldings    = {};    // { fund_id: amount_jpy }
let pfOrigins     = {};    // { fund_id: { amount, date, usdjpy } }
let pfLastDate    = null;  // 最終自動更新日 "YYYY-MM-DD"
let customFunds   = [];    // [{ id, label, bm, fx, amount }]
let pfCash        = 0;     // 現金預金（円、値動きなし）
let pfOriginBm    = {};    // { fund_id: ベンチマークの入力時(origin)終値 }

// 今日のセッション開始を示す垂直補助線プラグイン
Chart.register({
  id: 'todayLine',
  afterDatasetsDraw(chart) {
    const idx = chart.options.plugins?.todayLine?.index;
    if (idx == null || idx < 0) return;
    const pts = chart.getDatasetMeta(0)?.data;
    if (!pts || !pts[idx]) return;
    const x   = pts[idx].x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.strokeStyle = '#8b949e';
    ctx.lineWidth   = 1;
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.restore();
  }
});

// ── データ更新 ────────────────────────────────────────────────────────────────
async function doRefresh() {
  clearTimeout(autoTimer);
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = '読込中...';
  document.getElementById('spinner').classList.add('show');

  try {
    const resp = await fetch(`/api/fetch_all?period_idx=${currentPeriod}&chart_symbol=${encodeURIComponent(chartSymbol)}`);
    if (!resp.ok) throw new Error('fetch_all failed');
    const data = await resp.json();
    latestQuotes = data.quotes;
    updateCards(data.quotes, data.symbols_open, data.period_changes);
    if (pfOpen) renderPortfolio();
    lastChartArgs = [data.history, data.quotes[chartSymbol],
                     !data.is_open ? data.quotes['ES=F'] : null];
    updateChart(...lastChartArgs);
    updateHeader(data.is_open, data.status, data.updated);
    updateNews();
    const ms = data.is_open ? 5*60*1000 : 30*60*1000;
    autoTimer = setTimeout(doRefresh, ms);
  } catch(e) { console.error('refresh error', e); }

  btn.disabled = false;
  btn.textContent = '↻ 更新';
  document.getElementById('spinner').classList.remove('show');
}

// ── 対数スケール切り替え ──────────────────────────────────
function toggleLogScale() {
  logScale = !logScale;
  const btn = document.getElementById('log-btn');
  btn.classList.toggle('active', logScale);
  if (lastChartArgs) updateChart(...lastChartArgs);
}

// ── カード更新 ────────────────────────────────────────────────────────────────
function updateCards(quotes, symbolsOpen, periodChanges) {
  CARD_SYMS.forEach((sym, i) => {
    const q        = quotes[sym];
    const el       = document.getElementById('card-' + CARD_IDS[i]);
    if (!el) return;
    const priceEl  = el.querySelector('.card-price');
    const changeEl = el.querySelector('.card-change');
    // 終値バッジは市場状態が変わるので毎回作り直す
    el.querySelectorAll('.close-badge').forEach(b => b.remove());
    // ES=F: NYSE開場中はグレーアウト（現物が動く間は先物は脇役）
    // 他: 各市場クローズ中はグレーアウト
    const isGrayed = sym === 'ES=F' ? !!symbolsOpen['^GSPC'] : !symbolsOpen[sym];
    el.classList.toggle('card-closed', isGrayed);
    if (!q || q.price === null) {
      priceEl.textContent  = '---';
      priceEl.className    = 'card-price na';
      changeEl.textContent = '取得失敗';
      changeEl.className   = 'card-change na';
      prevQuotes[sym] = '---';
      return;
    }
    const isFX = FX_SYMS.has(sym);
    const decimals = 2;
    const priceStr = q.price.toLocaleString('ja-JP',
      {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
    priceEl.textContent = priceStr;
    priceEl.style.color = COLORS[i];

    // そのシンボルの市場がクローズ中は常時「終値」を表示
    if (!symbolsOpen[sym]) {
      const closeBadge = document.createElement('span');
      closeBadge.className = 'close-badge';
      closeBadge.textContent = '終値';
      el.appendChild(closeBadge);
    }
    // グレーアウト中はNEW!を出さない（既存バッジも消す）
    if (isGrayed) {
      el.querySelectorAll('.new-badge').forEach(b => b.remove());
    } else if (sym in prevQuotes && prevQuotes[sym] !== priceStr) {
      el.querySelectorAll('.new-badge').forEach(b => b.remove());
      const badge = document.createElement('span');
      badge.className = 'new-badge';
      badge.textContent = 'NEW!';
      el.appendChild(badge);
      setTimeout(() => badge.remove(), 30000);
    }
    prevQuotes[sym] = priceStr;

    if (currentPeriod === 0) {
      // 1日: 前日比をそのまま表示
      const up    = q.change >= 0;
      const arrow = up ? '▲' : '▼';
      const fxLabel = FX_SYMS.has(sym) ? ('  ' + (up ? '円安' : '円高')) : '';
      changeEl.textContent = `${arrow} ${Math.abs(q.change).toFixed(decimals)}  (${Math.abs(q.pct).toFixed(2)}%)${fxLabel}`;
      changeEl.className   = 'card-change ' + (up ? 'up' : 'dn');
    } else {
      // 選択期間の変化率のみ表示
      const pc = periodChanges ? periodChanges[sym] : null;
      if (pc !== null && pc !== undefined) {
        const pUp    = pc >= 0;
        const pArrow = pUp ? '▲' : '▼';
        const pFx    = FX_SYMS.has(sym) ? ('  ' + (pUp ? '円安' : '円高')) : '';
        changeEl.textContent = `${pArrow} ${Math.abs(pc).toFixed(2)}%${pFx}`;
        changeEl.className   = 'card-change ' + (pUp ? 'up' : 'dn');
      } else {
        changeEl.textContent = '---';
        changeEl.className   = 'card-change na';
      }
    }

    const periodEl = el.querySelector('.card-period');
    if (periodEl) periodEl.textContent = '';
  });
}

// ── ヘッダー更新 ──────────────────────────────────────────────────────────────
function updateHeader(isOpen, status, updated) {
  const el  = document.getElementById('market-status');
  el.textContent = (isOpen ? '● ' : '○ ') + status;
  el.style.color = isOpen ? '#3fb950' : '#8b949e';
  document.getElementById('updated').textContent = '更新: ' + updated + ' JST';
}

// ── カード選択 ────────────────────────────────────────────────────────────────
function selectCard(sym, cardEl, name) {
  chartSymbol  = sym;
  chartSymName = name;
  document.querySelectorAll('.card').forEach(c => c.classList.remove('selected'));
  cardEl.classList.add('selected');
  doRefresh();
}

// ── 期間選択 ──────────────────────────────────────────────────────────────────
function changePeriod(idx) {
  currentPeriod = idx;
  document.querySelectorAll('.period-btn:not(#log-btn)').forEach((b, i) =>
    b.classList.toggle('active', i === idx));
  doRefresh();
}

// ── チャート描画 ──────────────────────────────────────────────────────────────
const PERIOD_REF_LABELS = ['前日終値','1週前','1ヶ月前','3ヶ月前','6ヶ月前','年初','1年前','5年前','設定来'];

function updateChart(hist, symQ, futuresQ) {
  if (!hist || hist.labels.length === 0) return;

  const labels        = hist.labels;
  const values        = hist.values;
  const todayStartIdx = (currentPeriod === 0 && hist.today_start_index != null)
                        ? hist.today_start_index : -1;
  const first  = values[0];
  const last   = values[values.length - 1];

  // 1日は前日終値を基準、それ以外は期間最初の値を基準
  const refPrice = (currentPeriod === 0 && symQ && symQ.change !== null)
    ? Math.round((symQ.price - symQ.change) * 100) / 100
    : first;

  const up     = last >= refPrice;
  const symIdx = CARD_SYMS.indexOf(chartSymbol);
  const symColor = symIdx >= 0 ? COLORS[symIdx] : (up ? '#3fb950' : '#f85149');

  const datasets = [{
    label: chartSymName,
    data: values,
    borderColor: symColor,
    borderWidth: 2,
    pointRadius: 0,
    fill: false,
    tension: 0.1,
  }];

  // 基準線（点線）
  datasets.push({
    label: '_ref',
    data: labels.map(() => refPrice),
    borderColor: '#484f58',
    borderWidth: 1,
    borderDash: [5, 5],
    pointRadius: 0,
    fill: false,
    tension: 0,
  });

  // 先物ライン（クローズ時・S&P500選択中のみ）
  if (futuresQ && futuresQ.price !== null && chartSymbol === '^GSPC') {
    datasets.push({
      label: '先物',
      data: labels.map((_, i) => i === labels.length-1 ? futuresQ.price : null),
      borderColor: '#e3b341', borderWidth: 0,
      pointRadius: 8, pointStyle: 'circle',
      pointBackgroundColor: '#e3b341', spanGaps: false,
    });
  }

  const isFX = FX_SYMS.has(chartSymbol);
  const cfg = {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        todayLine: { index: todayStartIdx },
        legend: { display: false },
        tooltip: {
          backgroundColor: '#21262d', borderColor: '#30363d', borderWidth: 1,
          titleColor: '#8b949e', bodyColor: '#c9d1d9',
          filter: item => item.dataset.label !== '_ref',
          callbacks: {
            title: items => {
              const lbl = items[0]?.label || '';
              // "YYYY-MM-DD HH:MM" → "MM/DD HH:MM" で表示
              if (currentPeriod === 0 && lbl.length > 10)
                return lbl.slice(5, 7) + '/' + lbl.slice(8, 16);
              return lbl;
            },
            label: ctx => {
              const v = ctx.parsed.y;
              return ' ' + (isFX
                ? v.toFixed(2) + ' 円'
                : v.toLocaleString('ja-JP', {minimumFractionDigits:2, maximumFractionDigits:2}));
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: '#30363d' },
          ticks: {
            color: '#8b949e', maxTicksLimit: 10, maxRotation: 30,
            callback: function(val) {
              const lbl = this.getLabelForValue(val);
              if (!lbl) return '';
              // 1日: "YYYY-MM-DD HH:MM" → "HH:MM"
              if (currentPeriod === 0) return lbl.length > 10 ? lbl.substring(11, 16) : lbl;
              if (currentPeriod >= 7) return lbl.slice(0, 4);
              if (currentPeriod >= 5) return lbl.slice(2,4) + '/' + lbl.slice(5,7);
              return lbl.slice(5,7) + '/' + lbl.slice(8,10);
            },
          },
        },
        y: {
          type: logScale ? 'logarithmic' : 'linear',
          position: 'right', grid: { color: '#30363d' },
          ticks: {
            color: '#8b949e',
            callback: v => isFX
              ? v.toFixed(1) + '円'
              : v.toLocaleString('ja-JP', {maximumFractionDigits:0}),
          },
        },
      },
    },
  };

  // チャートタイトル
  const pct     = ((last - refPrice) / refPrice * 100).toFixed(2);
  const sign    = up ? '+' : '';
  const refLabel = PERIOD_REF_LABELS[currentPeriod];
  const refStr  = isFX
    ? refPrice.toFixed(2) + ' 円'
    : refPrice.toLocaleString('ja-JP', {minimumFractionDigits:2, maximumFractionDigits:2});
  const titleEl = document.getElementById('chart-title');
  const priceStr = isFX
    ? last.toFixed(2) + ' 円'
    : last.toLocaleString('ja-JP', {minimumFractionDigits:2, maximumFractionDigits:2});
  titleEl.innerHTML =
    `<span style="color:#8b949e;font-weight:400;margin-right:8px">${chartSymName}</span>`
    + `<span style="color:${symColor}">${priceStr}　${sign}${pct}%</span>`
    + `<span style="color:#6e7681;font-size:10px;margin-left:12px">${refLabel}: ${refStr}</span>`;
  document.getElementById('chart-area').style.borderColor = symColor;

  if (chart) {
    chart.data    = cfg.data;
    chart.options = cfg.options;
    chart.update('none');
  } else {
    chart = new Chart(document.getElementById('myChart').getContext('2d'), cfg);
  }
}

// ── ニューステッカー ──────────────────────────────────────────────────────────
async function updateNews() {
  try {
    const resp  = await fetch('/api/fetch_news');
    if (!resp.ok) throw new Error('fetch_news failed');
    const items = await resp.json();
    if (!items.length) return;

    const sep  = '<span class="ticker-dot"> ◆ </span>';
    const html  = items.map(t => `<span class="ticker-item">${escHtml(t)}</span>`).join(sep);
    const track = document.getElementById('ticker-track');
    // 2回分並べてシームレスループ
    track.innerHTML = html + sep + html + sep;

    // コンテンツ長に応じてアニメーション速度調整 (平均8文字/秒)
    const totalChars = items.join('').length;
    const duration   = Math.max(40, Math.round(totalChars / 8));
    track.style.animationDuration = duration + 's';
  } catch(e) { console.error('news error', e); }

  // 30分ごとに更新
  clearTimeout(newsTimer);
  newsTimer = setTimeout(updateNews, 30 * 60 * 1000);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── ポートフォリオ ──────────────────────────────────────────────────────────────
const FUND_CFG = [
  {id:'rakuten_vti',   label:'楽天・VTI',        bm:'^GSPC',  fx:'JPY=X'},
  {id:'rakuten_acwi',  label:'楽天・オルカン',    bm:'2559.T', fx:null},
  {id:'rakuten_sp500', label:'楽天・S&P500',      bm:'^GSPC',  fx:'JPY=X'},
  {id:'rakuten_ndx',   label:'楽天・NASDAQ-100',  bm:'^NDX',   fx:'JPY=X'},
  {id:'emaxis_nikkei', label:'eMAXIS Slim 日経平均',   bm:'^N225',  fx:null},
  {id:'emaxis_sp500',  label:'eMAXIS Slim S&P500',     bm:'^GSPC',  fx:'JPY=X'},
  {id:'emaxis_acwi',   label:'eMAXIS Slim オルカン',   bm:'2559.T', fx:null},
];

async function loadPortfolio() {
  try {
    const r = await fetch('/api/portfolio');
    const d = await r.json();
    pfHoldings = {};
    pfOrigins  = {};
    (d.holdings||[]).forEach(h => {
      if (h.amount > 0) pfHoldings[h.id] = h.amount;
      if (h.origin_date) pfOrigins[h.id] = {
        amount: h.origin_amount || h.amount,
        date:   h.origin_date,
        usdjpy: h.origin_usdjpy || null,
      };
    });
    customFunds = (d.custom_funds||[]).map(f => ({
      id: f.id, label: f.label, bm: f.bm, fx: f.fx || null,
    }));
    pfLastDate = d.last_date || null;
    pfCash     = d.cash || 0;
  } catch(e) {}
  await refreshOriginCloses();
}

async function savePortfolio() {
  const todayJST = new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Tokyo'});
  const holdings = FUND_CFG.map(f => {
    const o = pfOrigins[f.id] || {};
    return {
      id:            f.id,
      amount:        pfHoldings[f.id] || 0,
      origin_amount: o.amount || pfHoldings[f.id] || 0,
      origin_date:   o.date   || todayJST,
      origin_usdjpy: o.usdjpy || null,
    };
  });
  try {
    await fetch('/api/portfolio', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({holdings, custom_funds: customFunds, last_date: todayJST, cash: pfCash}),
    });
    pfLastDate = todayJST;
  } catch(e) {}
}

// 各銘柄のベンチマークについて「入力時(origin_date)の終値」をバックエンドから取得する。
// 入力時起点の累積騰落を出すための基準値。originは銘柄編集時しか変わらないため、
// 起動時・編集後にのみ呼べばよい（毎リフレッシュでは呼ばない）。
async function refreshOriginCloses() {
  const queries = [...FUND_CFG, ...customFunds]
    .filter(f => pfOrigins[f.id] && pfOrigins[f.id].date && f.bm)
    .map(f => ({key: f.id, symbol: f.bm, since: pfOrigins[f.id].date}));
  if (!queries.length) { pfOriginBm = {}; return; }
  try {
    const r = await fetch('/api/origin_closes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({queries}),
    });
    pfOriginBm = await r.json();
  } catch(e) { /* 取得失敗時は前回値を保持 */ }
}

function togglePortfolio() {
  pfOpen = !pfOpen;
  if (!pfOpen && pfEdit) {
    pfEdit = false;
    document.getElementById('pf-edit-btn').textContent = '編集';
  }
  document.getElementById('pf-overlay').classList.toggle('show', pfOpen);
  document.getElementById('pf-panel').classList.toggle('show', pfOpen);
  document.getElementById('pf-toggle-btn').classList.toggle('active', pfOpen);
  if (pfOpen) renderPortfolio();
}

function togglePfEdit() {
  if (pfEdit) {
    const todayJST  = new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Tokyo'});
    const usdJpy    = latestQuotes?.['JPY=X']?.price ?? null;
    [...FUND_CFG, ...customFunds].forEach(f => {
      const inp = document.getElementById('pf-inp-'+f.id);
      if (!inp) return;
      const v = parseInt(inp.value.replace(/[,，\s]/g,''), 10);
      if (!isNaN(v) && v > 0) {
        pfHoldings[f.id] = v;
        pfOrigins[f.id] = {
          amount: v,
          date:   todayJST,
          usdjpy: usdJpy ? Math.round(usdJpy * 100) / 100 : null,
        };
      } else if (inp.value.trim() === '') {
        delete pfHoldings[f.id];
        delete pfOrigins[f.id];
      }
    });
    const cashInp = document.getElementById('pf-inp-cash');
    if (cashInp) {
      const cv = parseInt(cashInp.value.replace(/[,，\s]/g,''), 10);
      pfCash = (!isNaN(cv) && cv > 0) ? cv : 0;
    }
    savePortfolio();
    pfEdit = false;
    document.getElementById('pf-edit-btn').textContent = '編集';
    renderPortfolio();
    refreshOriginCloses().then(renderPortfolio);  // 入力時(date/額)が変わるので基準を取り直す
    return;
  } else {
    pfEdit = true;
    document.getElementById('pf-edit-btn').textContent = '保存';
  }
  renderPortfolio();
}

function addCustomFund() {
  const labelEl  = document.getElementById('pf-new-label');
  const bmEl     = document.getElementById('pf-new-bm');
  const fxEl     = document.getElementById('pf-new-fx');
  const amountEl = document.getElementById('pf-new-amount');
  const label  = labelEl.value.trim();
  const bm     = bmEl.value;
  const fx     = fxEl.value || null;
  const amount = parseInt(amountEl.value.replace(/[,，\s]/g,''), 10);
  if (!label) { labelEl.focus(); return; }
  if (!bm)    { bmEl.focus();    return; }
  const id = 'custom_' + Date.now();
  customFunds.push({id, label, bm, fx});
  if (!isNaN(amount) && amount > 0) {
    const todayJST = new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Tokyo'});
    const usdJpy   = latestQuotes?.['JPY=X']?.price ?? null;
    pfHoldings[id] = amount;
    pfOrigins[id]  = {
      amount,
      date:   todayJST,
      usdjpy: usdJpy ? Math.round(usdJpy * 100) / 100 : null,
    };
  }
  savePortfolio();
  labelEl.value = ''; amountEl.value = '';
  renderPortfolio();
  refreshOriginCloses().then(renderPortfolio);  // 追加銘柄の入力時基準を取得
}

function removeCustomFund(id) {
  customFunds = customFunds.filter(f => f.id !== id);
  delete pfHoldings[id];
  delete pfOrigins[id];
  savePortfolio();
  renderPortfolio();
}

function estPct(f) {
  if (!latestQuotes) return null;
  const bq = latestQuotes[f.bm];
  if (!bq || bq.pct == null) return null;
  let p = bq.pct;
  if (f.fx) {
    const fq = latestQuotes[f.fx];
    if (fq && fq.pct != null)
      p = ((1 + p/100) * (1 + fq.pct/100) - 1) * 100;
  }
  return p;
}

// 入力時(origin_date)起点の累積騰落率(%)。est = origin_amount × (1 + これ/100)。
// ベンチマークの origin終値→現在価格 の比に、米国株系は為替(origin→現在)の比を合成する。
// 破壊的な日次複利を使わず毎回ここから再計算するため、アプリを毎日開かなくてもずれず、
// マネフォ実額を再入力すれば自動で校正される。
function cumPct(f) {
  if (!latestQuotes) return null;
  const o  = pfOrigins[f.id];
  const bq = latestQuotes[f.bm];
  const ob = pfOriginBm[f.id];
  if (!o || !bq || bq.price == null || !ob) return null;
  const ratio = bq.price / ob;
  // 分割規模(>5倍/<0.2倍)の比率は未補正の株式分割・データ異常の疑い。
  // 暴れた評価額を出さず null を返し、呼び出し側で base(入力時額)にフォールバックさせる。
  if (ratio > 5 || ratio < 0.2) return null;
  let p = ratio - 1;
  if (f.fx) {
    const fq = latestQuotes[f.fx];
    if (fq && fq.price != null && o.usdjpy) p = (1 + p) * (fq.price / o.usdjpy) - 1;
    else return null;
  }
  return p * 100;
}

function renderPortfolio() {
  const body = document.getElementById('pf-body');
  let totalBase = 0, totalEst = 0, hasData = false;

  const allFunds = [...FUND_CFG, ...customFunds];
  const html = allFunds.map(f => {
    const o    = pfOrigins[f.id];
    const base = (o && o.amount) ? o.amount : (pfHoldings[f.id] || 0);  // 入力時評価額が基準
    const dPct = estPct(f);   // 本日の単日騰落（バッジ表示用）
    const cPct = cumPct(f);   // 入力時起点の累積騰落（評価額の算出用）
    const est  = (base > 0 && cPct != null) ? Math.round(base * (1 + cPct/100))
               : (base > 0 ? base : null);
    const isCustom = customFunds.includes(f);

    if (base > 0) {
      hasData = true;
      totalBase += base;
      totalEst  += (est != null ? est : base);  // est欠損時もbaseで補完し合計から脱落させない
    }

    if (pfEdit) {
      return `<div class="pf-row">
        <div class="pf-row-name" style="display:flex;align-items:center;gap:6px">
          <span>${escHtml(f.label)}</span>
          ${isCustom ? `<button class="pf-del-btn" onclick="removeCustomFund('${f.id}')">削除</button>` : ''}
        </div>
        <input id="pf-inp-${f.id}" class="pf-inp" type="text"
          value="${base > 0 ? base.toLocaleString('ja-JP') : ''}"
          placeholder="マネフォの評価額（円）">
      </div>`;
    }

    if (!base) return `<div class="pf-row">
      <div class="pf-row-name" style="color:#484f58">${escHtml(f.label)}
        <span style="font-size:10px"> — 未設定</span></div>
    </div>`;

    const pctStr  = dPct != null ? `${dPct>=0?'+':''}${dPct.toFixed(2)}%` : '---';
    const cls     = dPct != null ? (dPct >= 0 ? 'up' : 'dn') : 'na';
    const estStr  = est != null ? `¥${est.toLocaleString('ja-JP')}` : `¥${base.toLocaleString('ja-JP')}`;

    let originRow = '';
    if (o && o.amount) {
      const oDiff = est != null ? est - o.amount : null;
      const oPct  = oDiff != null ? oDiff / o.amount * 100 : null;
      const oCls  = oDiff != null ? (oDiff >= 0 ? 'up' : 'dn') : 'na';
      const oDate = o.date ? o.date.slice(5).replace('-','/') : '—';
      const oFx   = o.usdjpy ? ` ¥${o.usdjpy.toFixed(2)}/$` : '';
      const oDiffStr = oDiff != null
        ? `<span class="${oCls}">${oDiff>=0?'+':''}¥${Math.abs(oDiff).toLocaleString('ja-JP')} (${oDiff>=0?'+':''}${oPct.toFixed(2)}%)</span>`
        : '---';
      originRow = `<div class="pf-row-origin">入力時 (${oDate}${oFx}) → ${oDiffStr}</div>`;
    }

    return `<div class="pf-row">
      <div class="pf-row-name">${escHtml(f.label)}</div>
      <div class="pf-row-vals">
        <span class="pf-row-amt">${estStr}</span>
        <span class="pf-row-chg ${cls}">本日 ${pctStr}</span>
      </div>
      ${originRow}
    </div>`;
  }).join('');

  // 現金預金（ベンチマークなし・値動きなし）
  let cashHtml = '';
  if (pfEdit) {
    cashHtml = `<div class="pf-row">
      <div class="pf-row-name">💴 現金預金</div>
      <input id="pf-inp-cash" class="pf-inp" type="text"
        value="${pfCash > 0 ? pfCash.toLocaleString('ja-JP') : ''}"
        placeholder="現金・預金（円）">
    </div>`;
  } else if (pfCash > 0) {
    hasData = true;
    totalBase += pfCash;
    totalEst  += pfCash;
    cashHtml = `<div class="pf-row">
      <div class="pf-row-name">💴 現金預金</div>
      <div class="pf-row-vals">
        <span class="pf-row-amt">¥${pfCash.toLocaleString('ja-JP')}</span>
        <span class="pf-row-chg na">—</span>
      </div>
    </div>`;
  }

  const addForm = pfEdit ? `<div class="pf-add-area">
    <div class="pf-add-label" style="margin-bottom:6px;font-weight:700">＋ 銘柄を追加</div>
    <div class="pf-add-row">
      <div class="pf-add-label">銘柄名</div>
      <input id="pf-new-label" class="pf-inp" type="text" placeholder="例: SBI S&P500">
    </div>
    <div class="pf-add-row" style="margin-top:5px">
      <div class="pf-add-label">ベンチマーク</div>
      <select id="pf-new-bm" class="pf-sel" style="width:100%;margin-top:4px">
        <option value="^GSPC">S&P 500</option>
        <option value="^NDX">NASDAQ 100</option>
        <option value="^N225">日経平均株価</option>
        <option value="2559.T">オルカン</option>
        <option value="JPY=X">ドル円</option>
        <option value="EURJPY=X">ユーロ円</option>
      </select>
    </div>
    <div class="pf-add-row" style="margin-top:5px">
      <div class="pf-add-label">為替換算</div>
      <select id="pf-new-fx" class="pf-sel" style="width:100%;margin-top:4px">
        <option value="">なし（円建て）</option>
        <option value="JPY=X">USD/JPY換算あり（米国株系）</option>
      </select>
    </div>
    <div class="pf-add-row" style="margin-top:5px">
      <div class="pf-add-label">初期評価額（円、省略可）</div>
      <input id="pf-new-amount" class="pf-inp" type="text" placeholder="例: 1500000">
    </div>
    <div class="pf-add-actions">
      <button class="pf-btn" onclick="addCustomFund()" style="border-color:#58a6ff;color:#58a6ff">追加</button>
    </div>
  </div>` : '';

  body.innerHTML = html + cashHtml + addForm;

  const foot = document.getElementById('pf-foot');
  if (!pfEdit && hasData) {
    const displayTotal = totalEst > 0 ? totalEst : totalBase;
    const d   = totalEst > 0 ? totalEst - totalBase : 0;
    const p   = totalEst > 0 ? (totalEst/totalBase - 1)*100 : 0;
    const cls = d >= 0 ? 'up' : 'dn';
    document.getElementById('pf-foot-total').textContent = `¥${displayTotal.toLocaleString('ja-JP')}`;
    document.getElementById('pf-foot-chg').innerHTML = d !== 0
      ? `<span class="${cls}">${d>=0?'+':''}¥${Math.abs(d).toLocaleString('ja-JP')} (${d>=0?'+':''}${p.toFixed(2)}%)</span>`
      : '';
    const now = new Date();
    const mm  = now.toLocaleString('en-US', {timeZone:'Asia/Tokyo', month:'numeric'});
    const dd  = now.toLocaleString('en-US', {timeZone:'Asia/Tokyo', day:'numeric'});
    document.getElementById('pf-foot-note').textContent = `${mm}/${dd} 22:00 マネフォ更新予定の速報値`;
    foot.style.display = 'block';
  } else {
    foot.style.display = 'none';
  }
}

// ── 起動 ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  loadPortfolio().then(() => doRefresh());
});
</script>
</body>
</html>
"""

HTML = HTML.replace("__APP_VERSION__", APP_VERSION)


def _startup_log(msg: str) -> None:
    """exeと同じフォルダに startup_log.txt を書き出す（配布版のみ）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        log_path = os.path.join(os.path.dirname(sys.executable), "startup_log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


# ── ローカル HTTP サーバー ─────────────────────────────────────────────────────
_http_api: Optional["Api"] = None


class _ApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            ct = "text/html; charset=utf-8"
        elif parsed.path == "/api/fetch_all":
            pid = int(qs.get("period_idx", ["0"])[0])
            sym = qs.get("chart_symbol", ["^GSPC"])[0]
            body = _http_api.fetch_all(pid, sym).encode("utf-8")  # type: ignore[union-attr]
            ct = "application/json; charset=utf-8"
        elif parsed.path == "/api/fetch_news":
            body = _http_api.fetch_news().encode("utf-8")  # type: ignore[union-attr]
            ct = "application/json; charset=utf-8"
        elif parsed.path == "/api/portfolio":
            body = _http_api.load_portfolio().encode("utf-8")  # type: ignore[union-attr]
            ct = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path in ("/api/portfolio", "/api/origin_closes"):
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length).decode("utf-8")
            if self.path == "/api/portfolio":
                body = _http_api.save_portfolio(data).encode("utf-8")  # type: ignore[union-attr]
            else:
                body = _http_api.origin_closes(data).encode("utf-8")  # type: ignore[union-attr]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *_: object) -> None:
        pass  # サーバーログを抑制


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── エントリポイント ──────────────────────────────────────────────────────────
def main() -> None:
    import traceback
    import ctypes

    global _http_api

    _startup_log("=== 起動開始 ===")
    _startup_log(f"executable: {sys.executable}")
    _startup_log(f"platform: {sys.platform}")

    _http_api = Api()
    port = _find_free_port()
    _startup_log(f"HTTP server port: {port}")

    server = ThreadingHTTPServer(("127.0.0.1", port), _ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _startup_log("HTTP server started")

    try:
        from PySide6.QtWidgets import QApplication, QMainWindow
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QColor

        _startup_log("PySide6 imported OK")

        qt_app = QApplication(sys.argv)
        window = QMainWindow()
        window.setWindowTitle(f"S&P500 ウォッチャー v{APP_VERSION}")
        window.setMinimumSize(760, 540)
        window.resize(1060, 740)

        view = QWebEngineView()
        view.page().setBackgroundColor(QColor("#0d1117"))
        view.load(QUrl(f"http://127.0.0.1:{port}/"))
        window.setCentralWidget(view)
        window.show()

        _startup_log("Qt window shown")
        sys.exit(qt_app.exec())

    except Exception as e:
        _startup_log(f"ERROR: {type(e).__name__}: {e}")
        _startup_log(traceback.format_exc())
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                0,
                f"起動エラーが発生しました。\n\n"
                f"startup_log.txt を確認してください。\n\n"
                f"({type(e).__name__}: {str(e)[:200]})",
                "起動エラー",
                0x10,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
