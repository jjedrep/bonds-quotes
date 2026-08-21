#!/usr/bin/env python3
"""
Скринер под правила из СТРАТЕГИИ.md

Читает готовые stocks.json, quotes.json и кэш истории цен.
Считает то, что считается: фильтры, условия входа, календарь событий.

Что НЕ делает: не читает новости. Два условия из правил -
"в новостях нет объяснения" и "причина падения структурная" -
проверяются человеком. Скрипт только сужает список до двух-трёх бумаг.
"""

import csv
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

ISS = "https://iss.moex.com/iss"
UA = {"User-Agent": "screener/1.0"}
HIST_DIR = "cache_stocks"

# ---------------------------------------------------------------- правила

MAX_VOLATILITY = 80          # выше - не входим
MIN_TURNOVER = 10_000_000    # ниже - не входим
MIN_HISTORY_DAYS = 240
DROP_THRESHOLD = -20         # падение за месяц для условия 1
SECTOR_GAP = 12              # насколько бумага должна отстать от сектора
PANIC_THRESHOLD = -7         # падение индекса за неделю для условия 2
EVENT_WINDOW = 45            # горизонт календаря для условия 3
GAP_FAST_DAYS = 40           # быстрое закрытие дивидендного гэпа

# Чёрный список: компании в реструктуризации, дефолте или явном стрессе.
# Ведётся руками - автоматически это не определить.
BLACKLIST = {
    "SMLT": "долговой стресс, рефинансирование ~300 млрд в 2026",
    "KRKN": "остановка производства после повреждения установки",
}

# Заседания ЦБ. Проверять на cbr.ru - список неполный.
CB_MEETINGS = ["2026-09-11"]

# Отраслевая принадлежность. ISS её не отдаёт, ведём вручную.
SECTORS = {
    "SBER": "банки", "SBERP": "банки", "VTBR": "банки", "CBOM": "банки",
    "BSPB": "банки", "SVCB": "банки", "TCSG": "банки", "MOEX": "банки",
    "GAZP": "нефтегаз", "LKOH": "нефтегаз", "ROSN": "нефтегаз",
    "SNGS": "нефтегаз", "SNGSP": "нефтегаз", "TATN": "нефтегаз",
    "TATNP": "нефтегаз", "NVTK": "нефтегаз", "SIBN": "нефтегаз",
    "BANE": "нефтегаз", "BANEP": "нефтегаз", "TRNFP": "нефтегаз",
    "GMKN": "металлургия", "NLMK": "металлургия", "MAGN": "металлургия",
    "CHMF": "металлургия", "RUAL": "металлургия", "PLZL": "металлургия",
    "ALRS": "металлургия", "MTLR": "металлургия", "MTLRP": "металлургия",
    "RASP": "металлургия", "VSMO": "металлургия", "SELG": "металлургия",
    "MTSS": "телеком", "RTKM": "телеком", "RTKMP": "телеком",
    "MGNT": "ритейл", "FIVE": "ритейл", "LENT": "ритейл",
    "OZON": "ритейл", "FIXP": "ритейл", "BELU": "ритейл",
    "PIKK": "девелопмент", "SMLT": "девелопмент", "LSRG": "девелопмент",
    "ETLN": "девелопмент",
    "IRAO": "энергетика", "HYDR": "энергетика", "FEES": "энергетика",
    "UPRO": "энергетика", "MSNG": "энергетика", "LSNGP": "энергетика",
    "MRKV": "энергетика", "MRKU": "энергетика", "MRKP": "энергетика",
    "MRKC": "энергетика", "MSRS": "энергетика", "TGKA": "энергетика",
    "PHOR": "химия", "AKRN": "химия", "KZOS": "химия", "NKNC": "химия",
    "YDEX": "технологии", "VKCO": "технологии", "POSI": "технологии",
    "ASTR": "технологии", "SOFL": "технологии", "DIAS": "технологии",
    "AFLT": "транспорт", "NMTP": "транспорт", "FESH": "транспорт",
    "AFKS": "холдинги", "SFIN": "холдинги",
    "MDMG": "медицина", "GECO": "медицина", "APTK": "медицина",
}


def get_json(url):
    try:
        with urlopen(Request(url, headers=UA), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except (URLError, HTTPError) as e:
        print(f"  !! {url}: {e}", file=sys.stderr)
        return None


def as_dicts(block):
    if not block:
        return []
    return [dict(zip(block["columns"], row)) for row in block["data"]]


def load_history(secid):
    """История цен из кэша, собранного fetch_stocks.py"""
    path = os.path.join(HIST_DIR, f"{secid}.csv")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r["date"], float(r["close"])))
            except (TypeError, ValueError):
                continue
    return sorted(rows)


def pct_change(hist, days_back):
    """Изменение цены за N торговых дней."""
    if len(hist) <= days_back:
        return None
    return (hist[-1][1] / hist[-1 - days_back][1] - 1) * 100


# ---------------------------------------------------------------- фильтры

def hard_filters(stock):
    """Возвращает список причин НЕ входить. Пустой список = чисто."""
    reasons = []
    sid = stock["secid"]

    if sid in BLACKLIST:
        reasons.append(f"чёрный список: {BLACKLIST[sid]}")
    if stock.get("volatility") and stock["volatility"] > MAX_VOLATILITY:
        reasons.append(f"волатильность {stock['volatility']:.0f}% "
                       f"выше лимита {MAX_VOLATILITY}%")
    if (stock.get("turnover_rub") or 0) < MIN_TURNOVER:
        reasons.append(f"оборот {(stock.get('turnover_rub') or 0)/1e6:.1f} млн "
                       f"ниже {MIN_TURNOVER/1e6:.0f} млн")
    if stock.get("excluded"):
        reasons.append(stock["excluded"])
    return reasons


# ---------------------------------------------------------------- условия

def sector_moves(stocks, hist_cache):
    """Медианное изменение за месяц по каждой отрасли."""
    by_sector = {}
    for s in stocks:
        sec = SECTORS.get(s["secid"])
        if not sec:
            continue
        ch = pct_change(hist_cache.get(s["secid"], []), 21)
        if ch is not None:
            by_sector.setdefault(sec, []).append(ch)
    return {k: statistics.median(v) for k, v in by_sector.items() if v}


def cond_drop_without_reason(stock, hist, sectors_move):
    """Условие 1: упала сильно и заметно хуже своей отрасли."""
    ch = pct_change(hist, 21)
    if ch is None or ch > DROP_THRESHOLD:
        return None
    sec = SECTORS.get(stock["secid"])
    if not sec or sec not in sectors_move:
        return None
    gap = sectors_move[sec] - ch
    if gap < SECTOR_GAP:
        return None
    return (f"упала на {ch:.0f}% за месяц, отрасль «{sec}» "
            f"{sectors_move[sec]:+.0f}% — отстаёт на {gap:.0f} п.п.")


def cond_panic_rebound(stock, hist, index_week):
    """Условие 2: паника на рынке, бумага в числе сильно упавших."""
    if index_week is None or index_week > PANIC_THRESHOLD:
        return None
    ch = pct_change(hist, 5)
    if ch is None or ch > index_week - 3:
        return None
    return (f"индекс за неделю {index_week:+.1f}%, бумага {ch:+.1f}% — "
            f"кандидат на отскок")


def cond_event_ahead(stock, calendar, today):
    """Условие 3: известное событие в пределах 45 дней."""
    hits = [e for e in calendar
            if e.get("secid") == stock["secid"]
            and 0 < (date.fromisoformat(e["date"]) - today).days <= EVENT_WINDOW]
    if not hits:
        return None
    e = hits[0]
    days = (date.fromisoformat(e["date"]) - today).days
    return f"{e['what']} через {days} дн ({e['date']})"


def gap_closure_days(secid, hist):
    """Медиана: за сколько дней бумага отыгрывала дивидендный гэп."""
    data = get_json(f"{ISS}/securities/{secid}/dividends.json?iss.meta=off")
    if not data:
        return None
    prices = dict(hist)
    dates = [d for d, _ in hist]
    spans = []
    for d in as_dicts(data.get("dividends")):
        rd = d.get("registryclosedate")
        if not rd or rd not in prices:
            continue
        i = dates.index(rd)
        before = prices[dates[i - 1]] if i > 0 else None
        if not before:
            continue
        for j in range(i + 1, min(i + 400, len(dates))):
            if prices[dates[j]] >= before:
                spans.append(j - i)
                break
    return statistics.median(spans) if spans else None


# ---------------------------------------------------------------- календарь

def bond_calendar(bonds, today, horizon=90):
    """Купоны, погашения и оферты по облигациям из портфеля и выгрузки."""
    out = []
    for b in bonds:
        m = b.get("maturity")
        if not m:
            continue
        days = (date.fromisoformat(m) - today).days
        if 0 < days <= horizon:
            out.append({"date": m, "what": f"погашение {b['name']}",
                        "secid": b["secid"], "type": "погашение"})
        nc = b.get("next_coupon")
        if nc and nc != "0000-00-00":
            d = (date.fromisoformat(nc) - today).days
            if 0 < d <= horizon:
                out.append({"date": nc, "what": f"купон {b['name']}",
                            "secid": b["secid"], "type": "купон"})
    return out


def dividend_calendar(secids, today, horizon=90):
    """Объявленные дивидендные отсечки."""
    out = []
    for sid in secids:
        data = get_json(f"{ISS}/securities/{sid}/dividends.json?iss.meta=off")
        if not data:
            continue
        for d in as_dicts(data.get("dividends")):
            rd = d.get("registryclosedate")
            if not rd:
                continue
            try:
                days = (date.fromisoformat(rd) - today).days
            except ValueError:
                continue
            if 0 < days <= horizon:
                out.append({"date": rd, "secid": sid, "type": "дивиденды",
                            "what": f"отсечка {sid}, {d.get('value')} ₽"})
    return out


# ---------------------------------------------------------------- вывод

def to_markdown(today, calendar, candidates, blocked, regime, index_week):
    out = [f"# Скринер на {today}", ""]

    if regime:
        out.append(f"**Рынок:** {regime.get('verdict', '')}")
    if index_week is not None:
        out.append(f"**Индекс за неделю:** {index_week:+.2f}%")
    out.append("")

    out.append("## Календарь на 90 дней\n")
    if calendar:
        out.append("| Дата | Через | Событие |")
        out.append("|---|---|---|")
        for e in sorted(calendar, key=lambda x: x["date"]):
            days = (date.fromisoformat(e["date"]) - today).days
            out.append(f"| {e['date']} | {days} дн | {e['what']} |")
    else:
        out.append("_Событий не найдено._")

    out.append("\n## Кандидаты\n")
    if candidates:
        out.append("Сработали условия входа. **Требуется проверка новостей** — "
                   "без неё вход по правилам не разрешён.\n")
        for c in candidates:
            out.append(f"### {c['name']} ({c['secid']})\n")
            for r in c["reasons"]:
                out.append(f"- {r}")
            out.append(f"\nЦена {c['price']}, волатильность "
                       f"{c['volatility']:.0f}%, оборот "
                       f"{c['turnover_rub']/1e6:.0f} млн\n")
    else:
        out.append("_Ни одна бумага не прошла условия входа. "
                   "По правилам — не входим._")

    if blocked:
        out.append("\n## Отсеяны жёсткими фильтрами\n")
        for b in blocked[:15]:
            out.append(f"- **{b['name']}** — {'; '.join(b['reasons'])}")

    out.append("\n---\n*Скрипт проверяет только то, что считается. "
               "Условия «нет объяснения в новостях» и «причина структурная» "
               "проверяются вручную.*")
    return "\n".join(out)


# ---------------------------------------------------------------- главное

def main():
    today = date.today()

    if not os.path.exists("stocks.json"):
        print("Нет stocks.json - сначала запустите fetch_stocks.py",
              file=sys.stderr)
        sys.exit(1)

    stocks_data = json.load(open("stocks.json", encoding="utf-8"))
    stocks = [s for s in stocks_data["stocks"] if s.get("rank")]
    regime = stocks_data.get("market_regime")

    bonds = []
    if os.path.exists("quotes.json"):
        bonds = json.load(open("quotes.json", encoding="utf-8"))["bonds"]

    # изменение индекса за неделю - из истории, которую копит облигационный скрипт
    index_week = None
    if os.path.exists("history_index.csv"):
        rows = [r for r in csv.DictReader(open("history_index.csv",
                                               encoding="utf-8"))
                if r["index"] == "IMOEX"]
        if len(rows) >= 6:
            try:
                index_week = (float(rows[-1]["value"]) /
                              float(rows[-6]["value"]) - 1) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    hist_cache = {s["secid"]: load_history(s["secid"]) for s in stocks}
    sectors_move = sector_moves(stocks, hist_cache)
    print(f"  отраслей посчитано: {len(sectors_move)}")

    calendar = bond_calendar(bonds, today)
    for d in CB_MEETINGS:
        days = (date.fromisoformat(d) - today).days
        if 0 < days <= 90:
            calendar.append({"date": d, "what": "заседание ЦБ по ключевой ставке",
                             "secid": None, "type": "ЦБ"})

    # дивиденды тянем только по бумагам без стоп-факторов - экономим запросы
    clean = [s for s in stocks if not hard_filters(s)]
    calendar += dividend_calendar([s["secid"] for s in clean[:40]], today)
    print(f"  событий в календаре: {len(calendar)}")

    candidates, blocked = [], []
    for s in stocks:
        stop = hard_filters(s)
        if stop:
            blocked.append({**s, "reasons": stop})
            continue
        hist = hist_cache.get(s["secid"], [])
        reasons = []
        for fn in (
            lambda: cond_drop_without_reason(s, hist, sectors_move),
            lambda: cond_panic_rebound(s, hist, index_week),
            lambda: cond_event_ahead(s, calendar, today),
        ):
            r = fn()
            if r:
                reasons.append(r)
        if reasons:
            candidates.append({**s, "reasons": reasons})

    candidates.sort(key=lambda x: -len(x["reasons"]))

    json.dump({
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade_date": today.isoformat(),
        "index_week_pct": index_week,
        "calendar": sorted(calendar, key=lambda x: x["date"]),
        "candidates": candidates,
        "blocked_count": len(blocked),
    }, open("screener.json", "w", encoding="utf-8"),
        ensure_ascii=False, indent=2)

    open("screener.md", "w", encoding="utf-8").write(
        to_markdown(today, calendar, candidates, blocked, regime, index_week))

    print(f"Готово: кандидатов {len(candidates)}, "
          f"отсеяно фильтрами {len(blocked)}")
    for c in candidates:
        print(f"  {c['name']}: {'; '.join(c['reasons'])}")


if __name__ == "__main__":
    main()
