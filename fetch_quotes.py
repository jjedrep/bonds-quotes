#!/usr/bin/env python3
"""
Ежедневный снимок долгового рынка Мосбиржи.

Забирает:
  - все рублёвые ОФЗ
  - корпоративные облигации первого уровня листинга с приличным оборотом
  - индексы RGBI, RGBITR, IMOEX

Считает доходность по фактическим денежным потокам, с учётом комиссии и НДФЛ.
Расписания купонов кэшируются в папке cache/ - это экономит сотни запросов.

Результат: quotes.json, quotes.md, history.csv
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

ISS = "https://iss.moex.com/iss"
UA = {"User-Agent": "bonds-quotes/4.0"}

# ---------------------------------------------------------------- настройки

NDFL = 0.13          # ставка НДФЛ. Нерезидент - 0.30. Доход свыше 2.4 млн - 0.15
COMMISSION = 0.003   # комиссия брокера: 0.003 = 0.3%, 0.0005 = 0.05%

# Фильтр качества корпоративных выпусков.
# Кредитных рейтингов у ISS нет, поэтому отбираем косвенно.
LIST_LEVEL_MAX = 1        # 1 = только первый уровень листинга (самый строгий)
MIN_VOLUME_RUB = 3_000_000  # минимальный оборот за день - отсекает неликвид
MAX_YEARS = 5             # длиннее 5 лет корпораты не берём
MAX_CORP = 90             # потолок числа корпоратов, чтобы не долбить ISS

CACHE_DIR = "cache"
CACHE_DAYS = 7            # как часто перечитывать расписание купонов

INDICES = ["RGBI", "RGBITR", "IMOEX"]


# ---------------------------------------------------------------- утилиты

def get_json(url, retries=3):
    """GET к ISS с повторами - сеть иногда моргает."""
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (URLError, HTTPError) as e:
            if attempt == retries - 1:
                print(f"  !! {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2)
    return None


def as_dicts(block):
    """Блок ISS {columns, data} -> список словарей."""
    if not block:
        return []
    return [dict(zip(block["columns"], row)) for row in block["data"]]


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- загрузка

def load_board(board):
    """Целая доска одним запросом: справочник + рыночные данные."""
    url = (f"{ISS}/engines/stock/markets/bonds/boards/{board}"
           f"/securities.json?iss.meta=off")
    data = get_json(url)
    if not data:
        return {}
    secs = {s["SECID"]: s for s in as_dicts(data.get("securities"))}
    mkt = {m["SECID"]: m for m in as_dicts(data.get("marketdata"))}
    for secid, s in secs.items():
        s.update({k: v for k, v in mkt.get(secid, {}).items() if v is not None})
        s["BOARD"] = board
    print(f"  {board}: {len(secs)} выпусков")
    return secs


def load_indices():
    """Индексы: RGBI, RGBITR, IMOEX."""
    url = f"{ISS}/engines/stock/markets/index/boards/SNDX/securities.json?iss.meta=off"
    data = get_json(url)
    out = {}
    if not data:
        return out
    secs = {s["SECID"]: s for s in as_dicts(data.get("securities"))}
    mkt = {m["SECID"]: m for m in as_dicts(data.get("marketdata"))}
    for name in INDICES:
        s, m = secs.get(name, {}), mkt.get(name, {})
        value = m.get("CURRENTVALUE") or m.get("LASTVALUE") or s.get("PREVPRICE")
        if value:
            out[name] = {
                "value": round(num(value), 2),
                "change_pct": round(num(m.get("LASTCHANGETOOPENPRC")
                                        or m.get("LASTCHANGEPRCNT")
                                        or m.get("CHANGE_PRC")), 2),
                "name": s.get("SHORTNAME") or name,
            }
    print(f"  индексы: {', '.join(out) or 'не получены'}")
    return out


def is_rub(row):
    """Юаневые выпуски ломают расчёт - номинал в другой валюте."""
    cur = (row.get("FACEUNIT") or row.get("CURRENCYID") or "SUR").upper()
    return cur in ("SUR", "RUB")


def years_left(row, today):
    d = row.get("MATDATE")
    if not d or d == "0000-00-00":
        return None
    try:
        return (datetime.strptime(d, "%Y-%m-%d").date() - today).days / 365.0
    except ValueError:
        return None


def pick_corporates(board, today):
    """Отбор качественных корпоратов: листинг, оборот, срок, не для квалов."""
    picked = []
    for secid, row in board.items():
        if not is_rub(row):
            continue
        if row.get("ISQUALIFIEDINVESTORS") in (1, "1"):
            continue
        level = row.get("LISTLEVEL")
        if level is None or int(num(level, 99)) > LIST_LEVEL_MAX:
            continue
        if num(row.get("VALTODAY")) < MIN_VOLUME_RUB:
            continue
        yl = years_left(row, today)
        if yl is None or not (0 < yl <= MAX_YEARS):
            continue
        picked.append((num(row.get("VALTODAY")), secid))
    picked.sort(reverse=True)
    result = [s for _, s in picked[:MAX_CORP]]
    print(f"  корпоратов после фильтра: {len(result)} "
          f"(листинг {LIST_LEVEL_MAX}, оборот от {MIN_VOLUME_RUB:,} руб)")
    return result


# ---------------------------------------------------------------- кэш купонов

def cashflows(secid, today):
    """Будущие купоны и погашения. Кэшируется - расписание меняется редко."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{secid}.json")

    raw = None
    if os.path.exists(path):
        try:
            cached = json.load(open(path, encoding="utf-8"))
            fetched = datetime.strptime(cached["fetched"], "%Y-%m-%d").date()
            if (today - fetched).days < CACHE_DAYS:
                raw = cached["data"]
        except Exception:
            raw = None

    if raw is None:
        url = (f"{ISS}/securities/{secid}/bondization.json"
               f"?iss.meta=off&limit=unlimited")
        data = get_json(url)
        if not data:
            return [], [], False
        raw = {"coupons": as_dicts(data.get("coupons")),
               "amortizations": as_dicts(data.get("amortizations"))}
        try:
            json.dump({"fetched": today.isoformat(), "data": raw},
                      open(path, "w", encoding="utf-8"), ensure_ascii=False)
        except OSError:
            pass

    coupons, unknown = [], False
    for c in raw["coupons"]:
        d = c.get("coupondate")
        if not d:
            continue
        d = datetime.strptime(d, "%Y-%m-%d").date()
        if d <= today:
            continue
        v = c.get("value_rub") or c.get("value")
        if v in (None, 0, "0"):
            unknown = True          # флоатер: купон ещё не объявлен
            continue
        coupons.append((d, float(v)))

    principal = []
    for a in raw["amortizations"]:
        d = a.get("amortdate")
        v = a.get("value_rub") or a.get("value")
        if d and v:
            d = datetime.strptime(d, "%Y-%m-%d").date()
            if d > today:
                principal.append((d, float(v)))

    return sorted(coupons), sorted(principal), unknown


# ---------------------------------------------------------------- расчёт

def xirr(flows, lo=-0.95, hi=10.0):
    """Эффективная годовая доходность методом деления пополам."""
    if len(flows) < 2:
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


def pick_price(row):
    for key in ("LAST", "MARKETPRICE", "LCLOSEPRICE", "WAPRICE", "PREVPRICE"):
        v = row.get(key)
        if v:
            return float(v), key
    return None, None


def analyse(row, today):
    secid = row["SECID"]
    face = num(row.get("FACEVALUE"), 1000)
    accint = num(row.get("ACCRUEDINT"))

    price_pct, src = pick_price(row)
    if price_pct is None:
        return None

    clean = price_pct / 100 * face
    commission = (clean + accint) * COMMISSION
    paid = clean + accint + commission

    coupons, principal, unknown = cashflows(secid, today)
    if not principal:
        return None

    base = {
        "secid": secid,
        "name": (row.get("SHORTNAME") or "").strip(),
        "isin": row.get("ISIN"),
        "board": row.get("BOARD"),
        "list_level": row.get("LISTLEVEL"),
        "price_pct": round(price_pct, 2),
        "price_source": src,
        "accrued_int": round(accint, 2),
        "maturity": max(d for d, _ in principal).isoformat(),
        "days_left": (max(d for d, _ in principal) - today).days,
        "volume_rub": row.get("VALTODAY"),
        "moex_yield": row.get("YIELD"),
        "moex_duration_days": row.get("DURATION"),
    }

    if unknown:
        base.update({"skipped": "флоатер: купоны не объявлены",
                     "ytm_bare": None, "ytm_gross": None, "ytm_net": None})
        return base

    coupon_sum = sum(v for _, v in coupons)
    principal_sum = sum(v for _, v in principal)
    profit = coupon_sum + principal_sum - paid
    tax = max(0.0, profit) * NDFL
    last_day = max(d for d, _ in principal)

    bare = [(today, -(clean + accint))] + coupons + principal
    gross = [(today, -paid)] + coupons + principal
    net = [(today, -paid)] + coupons + principal[:-1] + \
          [(last_day, principal[-1][1] - tax)]

    base.update({
        "cost_per_bond": round(paid, 2),
        "commission_rub": round(commission, 2),
        "tax_rub": round(tax, 2),
        "profit_rub": round(profit, 2),
        "ytm_bare": round((xirr(bare) or 0) * 100, 2),
        "ytm_gross": round((xirr(gross) or 0) * 100, 2),
        "ytm_net": round((xirr(net) or 0) * 100, 2),
        "skipped": None,
    })
    y = base["ytm_bare"]
    if y is not None and not (SANE_YIELD[0] <= y <= SANE_YIELD[1]):
        base["suspect"] = "доходность вне правдоподобного диапазона"
    return base


# Инфляционные ОФЗ (52xxx) дают РЕАЛЬНУЮ доходность - их нельзя
# смешивать с обычными. Амортизируемые 46xxx неликвидны и врут ценой.
LINKER_PREFIX = ("SU52",)
AMORT_PREFIX = ("SU46",)
SANE_YIELD = (8.0, 30.0)     # коридор правдоподобной доходности, %
CURVE_MIN_VOLUME = 1_000_000  # мёртвые выпуски в кривую не пускаем


def curve_point_ok(i):
    """Годится ли выпуск как опорная точка кривой."""
    if i["board"] != "TQOB" or i.get("skipped"):
        return False
    y = i.get("ytm_bare")
    if y is None or not (SANE_YIELD[0] <= y <= SANE_YIELD[1]):
        return False
    sid = i["secid"]
    if sid.startswith(LINKER_PREFIX) or sid.startswith(AMORT_PREFIX):
        return False
    if num(i.get("volume_rub")) < CURVE_MIN_VOLUME:
        return False
    return i["days_left"] > 0


def ofz_curve(items):
    """Кривая ОФЗ по медианам в корзинах - устойчива к выбросам."""
    import statistics
    buckets = [(0, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 5),
               (5, 7), (7, 10), (10, 15), (15, 40)]
    pts = []
    good = [i for i in items if curve_point_ok(i)]
    for lo, hi in buckets:
        ys = [i["ytm_bare"] for i in good if lo <= i["days_left"] / 365.0 < hi]
        # Меньше трёх выпусков - медиана недостоверна, корзину пропускаем.
        # Интерполяция перекинет мостик через пропуск.
        if len(ys) >= 3:
            pts.append(((lo + hi) / 2, statistics.median(ys)))
    print(f"  кривая построена по {len(good)} выпускам, {len(pts)} точек")
    return sorted(pts)


def curve_yield(curve, years):
    """Доходность ОФЗ на нужном сроке - линейной интерполяцией."""
    if not curve:
        return None
    if years <= curve[0][0]:
        return curve[0][1]
    if years >= curve[-1][0]:
        return curve[-1][1]
    for (x1, y1), (x2, y2) in zip(curve, curve[1:]):
        if x1 <= years <= x2:
            if x2 == x1:
                return y1
            return y1 + (y2 - y1) * (years - x1) / (x2 - x1)
    return None


# ---------------------------------------------------------------- вывод

def write_history(items, indices, today):
    """Дописывает строку за день. Через месяц будет с чем сравнивать."""
    new = not os.path.exists("history.csv")
    with open("history.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "secid", "name", "price_pct",
                        "ytm_bare", "ytm_net", "spread_bp"])
        for i in items:
            if i.get("ytm_bare") is not None:
                w.writerow([today, i["secid"], i["name"], i["price_pct"],
                            i["ytm_bare"], i["ytm_net"],
                            i.get("spread_bp", "")])

    new = not os.path.exists("history_index.csv")
    with open("history_index.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "index", "value", "change_pct"])
        for k, v in indices.items():
            w.writerow([today, k, v["value"], v["change_pct"]])


def to_markdown(items, indices, today):
    out = [f"# Долговой рынок на {today}",
           f"\nСнимок: {datetime.utcnow():%Y-%m-%d %H:%M} UTC  ",
           f"Комиссия {COMMISSION * 100:g}% · НДФЛ {NDFL * 100:g}%\n"]

    if indices:
        out.append("## Индексы\n")
        out.append("| Индекс | Значение | Изм. за день |")
        out.append("|---|---|---|")
        for k, v in indices.items():
            out.append(f"| {k} | {v['value']} | {v['change_pct']:+.2f}% |")
        out.append("")

    for board, title in (("TQOB", "ОФЗ"), ("TQCB", "Корпоративные")):
        rows = [i for i in items if i["board"] == board]
        if not rows:
            continue
        out.append(f"\n## {title}\n")
        out.append("| Бумага | Цена | НКД | Погашение | Дней | Без издержек | "
                   "Чистая | Спред к ОФЗ | Оборот |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for i in rows:
            if i.get("skipped"):
                out.append(f"| {i['name']} | {i['price_pct']} | "
                           f"{i['accrued_int']} | {i['maturity']} | "
                           f"{i['days_left']} | — | _{i['skipped']}_ | — | — |")
                continue
            sp = f"{i['spread_bp']:+.0f} б.п." if i.get("spread_bp") is not None else "—"
            vol = f"{num(i['volume_rub']) / 1e6:.1f} млн" if i.get("volume_rub") else "—"
            out.append(f"| {i['name']} | {i['price_pct']} | {i['accrued_int']} | "
                       f"{i['maturity']} | {i['days_left']} | {i['ytm_bare']}% | "
                       f"**{i['ytm_net']}%** | {sp} | {vol} |")

    out.append("\n*Спред — превышение доходности над кривой ОФЗ на том же сроке. "
               "Чистая доходность — после комиссии и НДФЛ.*")
    return "\n".join(out)


# ---------------------------------------------------------------- главное

def main():
    today = date.today()
    print(f"Снимок за {today}")

    print("Загружаю доски...")
    ofz = load_board("TQOB")
    corp = load_board("TQCB")
    indices = load_indices()

    ofz = {k: v for k, v in ofz.items() if is_rub(v)}
    corp_ids = pick_corporates(corp, today)

    targets = [(k, v) for k, v in ofz.items()]
    targets += [(s, corp[s]) for s in corp_ids]

    print(f"Считаю доходности: {len(targets)} выпусков "
          f"(расписания купонов берутся из кэша)...")
    items = []
    for secid, row in targets:
        try:
            r = analyse(row, today)
            if r:
                items.append(r)
        except Exception as e:
            print(f"  !! {secid}: {e}", file=sys.stderr)

    # спред к кривой ОФЗ
    curve = ofz_curve(items)
    for i in items:
        i["spread_bp"] = None
        if (i["board"] == "TQCB" and i.get("ytm_bare")
                and not i.get("suspect")
                and SANE_YIELD[0] <= i["ytm_bare"] <= SANE_YIELD[1]):
            ref = curve_yield(curve, i["days_left"] / 365.0)
            if ref:
                i["spread_bp"] = round((i["ytm_bare"] - ref) * 100, 0)

    items.sort(key=lambda x: (x["board"] != "TQOB",
                              -(x.get("ytm_net") or -99)))

    payload = {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade_date": today.isoformat(),
        "assumptions": {"ndfl": NDFL, "commission": COMMISSION,
                        "list_level_max": LIST_LEVEL_MAX,
                        "min_volume_rub": MIN_VOLUME_RUB},
        "indices": indices,
        "bonds": items,
    }
    json.dump(payload, open("quotes.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open("quotes.md", "w", encoding="utf-8").write(
        to_markdown(items, indices, today))
    write_history(items, indices, today)

    ok = sum(1 for i in items if not i.get("skipped"))
    print(f"Готово: {ok} посчитано, {len(items) - ok} пропущено (флоатеры)")


if __name__ == "__main__":
    main()
