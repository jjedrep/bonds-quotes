#!/usr/bin/env python3
"""
Снимок котировок облигаций с MOEX ISS + честный расчёт доходности
по реальным денежным потокам (с налогом и комиссией брокера).

Запуск: python fetch_quotes.py
Результат: quotes.json (для машины) и quotes.md (для чтения).
"""

import json
import sys
from datetime import date, datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

ISS = "https://iss.moex.com/iss"
BOARDS = ["TQOB", "TQCB"]          # TQOB - ОФЗ, TQCB - корпоративные
NDFL = 0.13                        # ставка НДФЛ
COMMISSION = 0.003                 # комиссия брокера, доля от суммы сделки
                                   # 0.003 = 0.3% (тариф "Инвестор" Т-Инвестиций)
                                   # 0.0005 = 0.05% (тариф "Трейдер")
UA = {"User-Agent": "bonds-quotes/1.0"}


def get_json(url):
    """GET с ISS, с понятной ошибкой вместо трейсбека."""
    try:
        with urlopen(Request(url, headers=UA), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except (URLError, HTTPError) as e:
        print(f"  !! не смог получить {url}: {e}", file=sys.stderr)
        return None


def as_dicts(block):
    """Блок ISS вида {columns: [...], data: [[...]]} -> список словарей."""
    if not block:
        return []
    cols = block["columns"]
    return [dict(zip(cols, row)) for row in block["data"]]


def load_watchlist(path="watchlist.txt"):
    """Читает список бумаг. Пустые строки и # - комментарии."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    out.append(line.upper())
    except FileNotFoundError:
        print("watchlist.txt не найден - выгружу все ОФЗ", file=sys.stderr)
    return out


def load_boards():
    """Тянет обе доски целиком, один запрос на доску."""
    rows = {}
    for board in BOARDS:
        url = (f"{ISS}/engines/stock/markets/bonds/boards/{board}"
               f"/securities.json?iss.meta=off")
        data = get_json(url)
        if not data:
            continue
        secs = {s["SECID"]: s for s in as_dicts(data.get("securities"))}
        mkt = {m["SECID"]: m for m in as_dicts(data.get("marketdata"))}
        for secid, s in secs.items():
            s.update({k: v for k, v in mkt.get(secid, {}).items()
                      if v is not None})
            s["BOARD"] = board
            rows[secid] = s
        print(f"  {board}: {len(secs)} бумаг")
    return rows


def pick_price(row):
    """Цена в % от номинала. Днём - последняя сделка, ночью - закрытие."""
    for key in ("LAST", "MARKETPRICE", "LCLOSEPRICE", "WAPRICE", "PREVPRICE"):
        v = row.get(key)
        if v:
            return float(v), key
    return None, None


def cashflows(secid, today):
    """Будущие купоны и погашения тела из справочника ISS."""
    url = f"{ISS}/securities/{secid}/bondization.json?iss.meta=off&limit=unlimited"
    data = get_json(url)
    if not data:
        return [], []

    coupons = []
    for c in as_dicts(data.get("coupons")):
        d, v = c.get("coupondate"), c.get("value_rub") or c.get("value")
        if d and v:
            d = datetime.strptime(d, "%Y-%m-%d").date()
            if d > today:
                coupons.append((d, float(v)))

    principal = []
    for a in as_dicts(data.get("amortizations")):
        d, v = a.get("amortdate"), a.get("value_rub") or a.get("value")
        if d and v:
            d = datetime.strptime(d, "%Y-%m-%d").date()
            if d > today:
                principal.append((d, float(v)))

    return sorted(coupons), sorted(principal)


def xirr(flows, lo=-0.95, hi=10.0):
    """Эффективная годовая доходность (IRR) методом деления пополам."""
    if not flows:
        return None
    t0 = flows[0][0]

    def npv(rate):
        return sum(v / (1 + rate) ** ((d - t0).days / 365.0) for d, v in flows)

    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def analyse(row, today):
    """Считает всё по одной бумаге."""
    secid = row["SECID"]
    face = float(row.get("FACEVALUE") or 1000)
    accint = float(row.get("ACCRUEDINT") or 0)

    price_pct, price_src = pick_price(row)
    if price_pct is None:
        return None

    clean = price_pct / 100 * face
    commission = (clean + accint) * COMMISSION
    paid = clean + accint + commission

    coupons, principal = cashflows(secid, today)
    if not principal:
        return None

    coupon_sum = sum(v for _, v in coupons)
    principal_sum = sum(v for _, v in principal)

    # НДФЛ: база = все поступления минус все затраты (НКД и комиссия - расход)
    profit = coupon_sum + principal_sum - paid
    tax = max(0.0, profit) * NDFL

    last_day = max(d for d, _ in principal)
    days = (last_day - today).days

    gross = [(today, -paid)] + coupons + principal
    net = [(today, -paid)] + coupons + principal[:-1] + \
          [(last_day, principal[-1][1] - tax)]

    return {
        "secid": secid,
        "name": (row.get("SHORTNAME") or "").strip(),
        "isin": row.get("ISIN"),
        "board": row.get("BOARD"),
        "price_pct": round(price_pct, 2),
        "price_source": price_src,
        "accrued_int": round(accint, 2),
        "cost_per_bond": round(paid, 2),
        "maturity": last_day.isoformat(),
        "days_left": days,
        "payments_left": len(coupons) + len(principal),
        "coupon_value": row.get("COUPONVALUE"),
        "next_coupon": row.get("NEXTCOUPON"),
        "profit_rub": round(profit, 2),
        "tax_rub": round(tax, 2),
        "commission_rub": round(commission, 2),
        "ytm_gross": round((xirr(gross) or 0) * 100, 2),
        "ytm_net": round((xirr(net) or 0) * 100, 2),
        "moex_yield": row.get("YIELD"),
        "duration_days": row.get("DURATION"),
        "volume_rub": row.get("VALTODAY"),
    }


def to_markdown(items, today):
    head = (f"# Котировки облигаций\n\n"
            f"Снимок: **{datetime.utcnow():%Y-%m-%d %H:%M} UTC**  \n"
            f"Комиссия в расчёте: {COMMISSION * 100:g}% · НДФЛ: {NDFL * 100:g}%\n\n"
            "| Бумага | Цена, % | НКД | Затраты | Погашение | Дней | "
            "Дох. до налога | Дох. чистая | YTM Мосбиржи |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    body = ""
    for i in items:
        body += (f"| {i['name']} ({i['secid']}) | {i['price_pct']} | "
                 f"{i['accrued_int']} | {i['cost_per_bond']} | "
                 f"{i['maturity']} | {i['days_left']} | "
                 f"{i['ytm_gross']}% | **{i['ytm_net']}%** | "
                 f"{i['moex_yield'] or '-'} |\n")
    tail = ("\n*Чистая доходность — после НДФЛ и комиссии, расчёт по фактическим "
            "денежным потокам. Расчёт от даты снимка, без учёта режима Т+1.*\n")
    return head + body + tail


def main():
    today = date.today()
    print(f"Загружаю доски ISS ({today})...")
    boards = load_boards()
    if not boards:
        print("Не получил данных с ISS.", file=sys.stderr)
        sys.exit(1)

    wanted = load_watchlist()
    if wanted:
        by_isin = {v.get("ISIN"): k for k, v in boards.items() if v.get("ISIN")}
        targets = []
        for w in wanted:
            if w in boards:
                targets.append(w)
            elif w in by_isin:
                targets.append(by_isin[w])
            else:
                print(f"  ? не нашёл на бирже: {w}", file=sys.stderr)
    else:
        targets = [k for k, v in boards.items() if v["BOARD"] == "TQOB"]

    print(f"Считаю доходности: {len(targets)} бумаг...")
    items = []
    for secid in targets:
        try:
            r = analyse(boards[secid], today)
            if r:
                items.append(r)
        except Exception as e:
            print(f"  !! {secid}: {e}", file=sys.stderr)

    items.sort(key=lambda x: x["ytm_net"], reverse=True)

    payload = {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade_date": today.isoformat(),
        "assumptions": {"ndfl": NDFL, "commission": COMMISSION},
        "bonds": items,
    }
    with open("quotes.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open("quotes.md", "w", encoding="utf-8") as f:
        f.write(to_markdown(items, today))

    print(f"Готово: {len(items)} бумаг -> quotes.json, quotes.md")


if __name__ == "__main__":
    main()
