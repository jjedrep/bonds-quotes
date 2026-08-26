#!/usr/bin/env python3
"""
Акции индекса Мосбиржи: факторы, ранжирование, самопроверка.

Что делает:
  1. Берёт состав индекса IMOEX и дневную историю цен (кэшируется)
  2. Считает факторы: импульс, разворот, положение в годовом диапазоне,
     волатильность, ликвидность
  3. Ранжирует бумаги, выделяет топ-5 и дно-5
  4. Записывает прогноз в predictions.csv
  5. Проверяет прогнозы месячной давности: сбылись или нет

Пятый пункт - самое важное. Без него это не эксперимент, а гадание.
"""

import csv
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

ISS = "https://iss.moex.com/iss"
UA = {"User-Agent": "stocks-factors/1.0"}

HIST_DIR = "cache_stocks"
HIST_YEARS = 2            # сколько истории тянуть при первом запуске
HORIZON_DAYS = 30         # горизонт прогноза - месяц
TOP_N = 5                 # размер групп "топ" и "дно"

# Берём все бумаги индекса. Оборот больше не фильтр, а справочная
# колонка - смотреть на неё стоит перед сделкой, а не при отборе.
MIN_TURNOVER = 10_000_000   # отсекает совсем неторгуемые бумаги

# Скачок цены за один день больше 25% - почти наверняка не рынок,
# а корпоративное действие: сплит, консолидация, допэмиссия.
# Такие бумаги из рейтинга убираем, иначе они всплывают наверх зря.
JUMP_THRESHOLD = 0.25


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except (URLError, HTTPError) as e:
            if attempt == retries - 1:
                print(f"  !! {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2)
    return None


def as_dicts(block):
    if not block:
        return []
    return [dict(zip(block["columns"], row)) for row in block["data"]]


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ состав индекса

def index_members():
    """Бумаги, входящие в IMOEX, с их весами."""
    url = (f"{ISS}/statistics/engines/stock/markets/index/analytics"
           f"/IMOEX.json?iss.meta=off&limit=100")
    data = get_json(url)
    out = {}
    if not data:
        return out
    for row in as_dicts(data.get("analytics")):
        sid = row.get("secids") or row.get("ticker")
        if sid:
            out[sid] = num(row.get("weight"), 0)
    print(f"  состав IMOEX: {len(out)} бумаг")
    return out


def board_snapshot():
    """Текущие цены и обороты по всей доске акций."""
    url = (f"{ISS}/engines/stock/markets/shares/boards/TQBR"
           f"/securities.json?iss.meta=off")
    data = get_json(url)
    if not data:
        return {}
    secs = {s["SECID"]: s for s in as_dicts(data.get("securities"))}
    mkt = {m["SECID"]: m for m in as_dicts(data.get("marketdata"))}
    for sid, s in secs.items():
        s.update({k: v for k, v in mkt.get(sid, {}).items() if v is not None})
    print(f"  доска TQBR: {len(secs)} бумаг")
    return secs


# ------------------------------------------------------------ история цен

def load_cache(secid):
    path = os.path.join(HIST_DIR, f"{secid}.csv")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = num(r.get("close"))
                if c:
                    rows.append((r["date"], c, num(r.get("volume"), 0)))
    return sorted(rows)


def save_cache(secid, rows):
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, f"{secid}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "close", "volume"])
        w.writerows(rows)


def fetch_history(secid, since, until):
    """Дневные свечи с ISS. Ответ постраничный, идём курсором."""
    rows, start = [], 0
    while True:
        url = (f"{ISS}/history/engines/stock/markets/shares/boards/TQBR"
               f"/securities/{secid}.json?iss.meta=off"
               f"&from={since}&till={until}&start={start}")
        data = get_json(url)
        if not data:
            break
        page = as_dicts(data.get("history"))
        if not page:
            break
        for r in page:
            close = num(r.get("CLOSE")) or num(r.get("LEGALCLOSEPRICE"))
            if r.get("TRADEDATE") and close:
                rows.append((r["TRADEDATE"], close, num(r.get("VALUE"), 0)))
        if len(page) < 100:
            break
        start += len(page)
        time.sleep(0.1)          # вежливость к ISS
    return rows


MIN_HISTORY = 240          # столько дней нужно для расчёта факторов


def history_for(secid, today):
    """История из кэша. Если кэш неполный - перекачиваем целиком."""
    cached = load_cache(secid)
    since = (today - timedelta(days=365 * HIST_YEARS)).isoformat()
    need_from = (today - timedelta(days=int(365 * 1.2))).isoformat()

    # Кэш считаем годным, только если он и длинный, и начинается достаточно
    # рано. Иначе оборванная загрузка навсегда оставила бы бумагу битой.
    cache_ok = (len(cached) >= MIN_HISTORY and cached[0][0] <= need_from)

    if cache_ok:
        last = cached[-1][0]
        if last >= today.isoformat():
            return cached
        fresh = fetch_history(secid, last, today.isoformat())
        seen = {d for d, _, _ in cached}
        merged = cached + [r for r in fresh if r[0] not in seen]
    else:
        if cached:
            print(f"    {secid}: кэш неполный ({len(cached)} дней) - "
                  f"качаю заново")
        merged = fetch_history(secid, since, today.isoformat())

    merged = sorted(set(merged))
    if merged:
        save_cache(secid, merged)
    return merged


# ------------------------------------------------------------ факторы

def find_jump(closes):
    """Ищет неправдоподобный дневной скачок за последний год."""
    tail = closes[-252:]
    for i in range(1, len(tail)):
        if tail[i - 1] and abs(tail[i] / tail[i - 1] - 1) > JUMP_THRESHOLD:
            return round((tail[i] / tail[i - 1] - 1) * 100, 1)
    return None


def factors(secid, hist, snap):
    """Считает факторы по одной бумаге. None - если данных не хватает."""
    if len(hist) < MIN_HISTORY:              # меньше года истории - пропуск
        return None

    closes = [c for _, c, _ in hist]
    hist_last = closes[-1]           # закрытие последнего дня в кэше
    hist_date = hist[-1][0]
    jump = find_jump(closes)

    # Историю ISS публикует с задержкой: вечером текущего дня свечи за
    # сегодня ещё нет. Поэтому цену берём из живой доски, а историю
    # используем только для расчёта факторов.
    live = None
    for key in ("LAST", "MARKETPRICE", "LCLOSEPRICE", "WAPRICE", "PREVPRICE"):
        v = snap.get(key)
        if v:
            live = float(v)
            break
    last = live if live else hist_last
    stale = (live is not None and hist_last and
             abs(live / hist_last - 1) > 0.0001)

    def ret(days_ago_from, days_ago_to=0):
        """Доходность между двумя точками в прошлом."""
        i1 = len(closes) - 1 - days_ago_from
        i2 = len(closes) - 1 - days_ago_to
        if i1 < 0 or i2 < 0:
            return None
        return (closes[i2] / closes[i1] - 1) * 100

    # Импульс 12-1: доходность за год, БЕЗ последнего месяца.
    # Последний месяц исключают намеренно - он ведёт себя обратно.
    momentum = ret(252, 21)

    # Краткосрочный разворот: сильное движение за месяц часто откатывается,
    # поэтому знак минус - чем выше рост, тем хуже ранг.
    reversal = ret(21)

    # Положение в годовом диапазоне: 100 = на максимуме за год
    year = closes[-252:]
    lo, hi = min(year), max(year)
    pos52 = (last - lo) / (hi - lo) * 100 if hi > lo else 50

    # Волатильность: годовое стандартное отклонение дневных изменений
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(-60, 0)]
    vol = statistics.pstdev(rets) * (252 ** 0.5) * 100 if len(rets) > 10 else None

    turnover = num(snap.get("VALTODAY"), 0) or 0

    if momentum is None or reversal is None or vol is None:
        return None

    return {
        "secid": secid,
        "name": (snap.get("SHORTNAME") or secid).strip(),
        "price": round(last, 4),
        "price_source": "живая доска" if live else "кэш истории",
        "hist_last_date": hist_date,
        "hist_last_close": hist_last,
        "price_moved_since_hist": round((last / hist_last - 1) * 100, 2)
                                  if (stale and hist_last) else 0.0,
        "jump_pct": jump,
        "excluded": ("скачок цены %+.0f%% за день - вероятно корпоративное "
                     "действие, а не рынок" % jump) if jump else None,
        "momentum_12_1": round(momentum, 2),
        "reversal_1m": round(reversal, 2),
        "pos_52w": round(pos52, 1),
        "volatility": round(vol, 1),
        "turnover_rub": turnover,
    }


def rank_all(all_items):
    """Ранжирование: складываем ранги по факторам. Устойчивее весов."""
    items = [i for i in all_items if not i.get("excluded")]
    n = len(items)
    if n < 10:
        return all_items

    def assign(key, reverse, label):
        order = sorted(items, key=lambda x: x[key], reverse=reverse)
        for pos, it in enumerate(order, 1):
            it.setdefault("ranks", {})[label] = pos

    assign("momentum_12_1", True, "импульс")      # выше импульс - лучше
    assign("reversal_1m", False, "разворот")      # ниже месячный рост - лучше
    assign("pos_52w", True, "у максимума")        # ближе к максимуму - лучше
    assign("volatility", False, "спокойствие")    # ниже волатильность - лучше

    for it in items:
        it["score"] = sum(it["ranks"].values())
        it["score_norm"] = round(100 * (1 - (it["score"] - 4) / (4 * n - 4)), 1)

    items.sort(key=lambda x: x["score"])
    for pos, it in enumerate(items, 1):
        it["rank"] = pos
    excluded = [i for i in all_items if i.get("excluded")]
    for i in excluded:
        i["rank"] = None
    return items + excluded


# ------------------------------------------------------------ эксперимент

def market_regime(items):
    """Честный ответ на вопрос: а есть ли вообще что покупать."""
    ranked = [i for i in items if i.get("rank")]
    if not ranked:
        return None
    up = sum(1 for i in ranked if i["momentum_12_1"] > 0)
    share = up / len(ranked)
    near_high = sum(1 for i in ranked if i["pos_52w"] > 70) / len(ranked)

    if share < 0.15:
        verdict = ("Падающий рынок. Растущих бумаг почти нет, "
                   "рейтинг показывает лишь кто упал меньше. "
                   "Покупать по импульсу нечего.")
    elif share < 0.40:
        verdict = ("Слабый рынок. Растёт меньшинство - "
                   "к сигналам относиться осторожно.")
    elif share < 0.70:
        verdict = "Смешанный рынок. Отбор имеет смысл."
    else:
        verdict = "Растущий рынок. Импульс работает лучше всего."

    return {
        "with_positive_momentum": up,
        "total": len(ranked),
        "share_up_pct": round(share * 100, 1),
        "share_near_52w_high_pct": round(near_high * 100, 1),
        "verdict": verdict,
        "tradeable": share >= 0.15,
    }


def log_prediction(items, today, imoex):
    """Записывает прогноз, чтобы через месяц было что проверять."""
    ranked = [i for i in items if i.get("rank")]
    top = [i["secid"] for i in ranked[:TOP_N]]
    bottom = [i["secid"] for i in ranked[-TOP_N:]]
    prices = {i["secid"]: i["price"] for i in items}

    new = not os.path.exists("predictions.csv")
    with open("predictions.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "group", "secid", "price", "imoex"])
        for s in top:
            w.writerow([today, "top", s, prices[s], imoex])
        for s in bottom:
            w.writerow([today, "bottom", s, prices[s], imoex])
    return top, bottom


def evaluate(today, current_prices, imoex_now):
    """Проверяет прогнозы месячной давности. Главная часть эксперимента."""
    if not os.path.exists("predictions.csv"):
        return None

    target = today - timedelta(days=HORIZON_DAYS)
    rows = list(csv.DictReader(open("predictions.csv", encoding="utf-8")))
    dates = sorted({r["date"] for r in rows})
    # ближайшая дата прогноза к нужной, но не позже
    past = [d for d in dates if d <= target.isoformat()]
    if not past:
        return None
    d0 = past[-1]

    res = {"prediction_date": d0, "horizon_days": (today - date.fromisoformat(d0)).days}
    for group in ("top", "bottom"):
        gains = []
        for r in rows:
            if r["date"] == d0 and r["group"] == group:
                now = current_prices.get(r["secid"])
                old = num(r["price"])
                if now and old:
                    gains.append((now / old - 1) * 100)
        res[group] = round(statistics.mean(gains), 2) if gains else None

    base = [num(r["imoex"]) for r in rows if r["date"] == d0]
    if base and base[0] and imoex_now:
        res["imoex"] = round((imoex_now / base[0] - 1) * 100, 2)

    if res.get("top") is not None and res.get("bottom") is not None:
        res["spread"] = round(res["top"] - res["bottom"], 2)
        res["verdict"] = ("система различает" if res["spread"] > 0
                          else "система не различает")
    return res


# ------------------------------------------------------------ вывод

def to_markdown(items, today, check, imoex, regime=None):
    out = [f"# Акции индекса Мосбиржи на {today}", ""]
    if imoex:
        out.append(f"IMOEX: **{imoex}**")
    out.append("")

    if regime:
        out.append("## Состояние рынка\n")
        out.append(f"**{regime['verdict']}**\n")
        out.append(f"Растущих бумаг (импульс за год выше нуля): "
                   f"{regime['with_positive_momentum']} из {regime['total']} "
                   f"({regime['share_up_pct']}%). "
                   f"У годовых максимумов: {regime['share_near_52w_high_pct']}%.\n")

    if check:
        out.append("## Проверка прошлого прогноза\n")
        out.append(f"Прогноз от {check['prediction_date']}, "
                   f"прошло {check['horizon_days']} дней:\n")
        out.append("| Группа | Результат |")
        out.append("|---|---|")
        out.append(f"| Топ-{TOP_N} | {check.get('top')}% |")
        out.append(f"| Дно-{TOP_N} | {check.get('bottom')}% |")
        out.append(f"| Индекс | {check.get('imoex')}% |")
        if "spread" in check:
            out.append(f"\n**Разница топ минус дно: {check['spread']} п.п. "
                       f"— {check['verdict']}**\n")

    excluded = [i for i in items if i.get("excluded")]
    items = [i for i in items if i.get("rank")]

    if items:
        hd = sorted({i.get("hist_last_date") for i in items
                     if i.get("hist_last_date")})
        if hd:
            out.append(f"\n_Факторы посчитаны по истории до **{hd[-1]}**. "
                       f"Цены — текущие, из биржевой доски. Расхождение "
                       f"нормально: ISS публикует дневные свечи с задержкой._\n")

    out.append("\n## Рейтинг\n")
    out.append("| # | Бумага | В индексе | Цена | Импульс 12-1 | Мес. изм. | "
               "Годовой диапазон | Волат. | Оборот |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for i in items:
        out.append(f"| {i['rank']} | {i['name']} | "
                   f"{'да' if i.get('in_index') else '—'} | {i['price']} | "
                   f"{i['momentum_12_1']:+.1f}% | {i['reversal_1m']:+.1f}% | "
                   f"{i['pos_52w']:.0f}% | {i['volatility']:.0f}% | "
                   f"{i['turnover_rub'] / 1e6:.0f} млн |")

    if excluded:
        out.append("\n## Исключены из рейтинга\n")
        for i in excluded:
            out.append(f"- **{i['name']}** — {i['excluded']}")
        out.append("")

    out.append("\n*Импульс 12-1 — доходность за год без последнего месяца. "
               "Годовой диапазон: 100% = на максимуме за год. "
               "Рейтинг — сумма рангов по четырём факторам, а не прогноз цены.*")
    return "\n".join(out)


# ------------------------------------------------------------ главное

def main():
    today = date.today()
    print(f"Акции, снимок за {today}")

    members = index_members()
    snap = board_snapshot()
    if not snap:
        print("Не получил доску акций", file=sys.stderr)
        sys.exit(1)

    idx = get_json(f"{ISS}/engines/stock/markets/index/boards/SNDX"
                   f"/securities.json?iss.meta=off")
    imoex = None
    if idx:
        for m in as_dicts(idx.get("marketdata")):
            if m.get("SECID") == "IMOEX":
                imoex = num(m.get("CURRENTVALUE") or m.get("LASTVALUE"))

    # Не полагаемся на эндпоинт состава индекса - он отдаёт список
    # неполностью. Берём всю доску акций и фильтруем по обороту.
    # Принадлежность к IMOEX остаётся пометкой, а не условием отбора.
    targets = [sid for sid, row in snap.items()
               if num(row.get("VALTODAY"), 0) >= MIN_TURNOVER]
    print(f"  состав IMOEX (справочно): {len(members)}")
    print(f"  вся доска: {len(snap)}, после фильтра по обороту: {len(targets)}")
    if len(members) < 30:
        print("  ! список индекса пришёл неполным - работаем по всей доске")

    print("  подгружаю историю цен (кэш экономит запросы)...")
    items, dropped = [], []
    for sid in targets:
        try:
            hist = history_for(sid, today)
            if len(hist) < MIN_HISTORY:
                dropped.append((sid, f"истории всего {len(hist)} дней"))
                continue
            f = factors(sid, hist, snap[sid])
            if f:
                f["in_index"] = sid in members
                items.append(f)
            else:
                dropped.append((sid, "не хватило данных для факторов"))
        except Exception as e:
            dropped.append((sid, f"ошибка: {e}"))
            print(f"  !! {sid}: {e}", file=sys.stderr)

    print(f"  в рейтинг попало: {len(items)}, отсеяно: {len(dropped)}")
    for sid, why in dropped:
        print(f"    - {sid}: {why}")

    if len(items) < 10:
        print(f"Слишком мало бумаг с историей ({len(items)}) - "
              f"рейтинг не строю", file=sys.stderr)
        sys.exit(1)

    items = rank_all(items)
    regime = market_regime(items)
    if regime:
        print(f"  {regime['verdict']}")
    prices = {i["secid"]: i["price"] for i in items}
    check = evaluate(today, prices, imoex)
    top, bottom = log_prediction(items, today, imoex)

    payload = {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade_date": today.isoformat(),
        "imoex": imoex,
        "horizon_days": HORIZON_DAYS,
        "evaluation": check,
        "market_regime": regime,
        "dropped": [{"secid": s, "reason": w} for s, w in dropped],
        "universe": {"index_members": len(members), "board": len(snap),
                     "targets": len(targets), "ranked": len(items)},
        "top": top,
        "bottom": bottom,
        "stocks": items,
    }
    json.dump(payload, open("stocks.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open("stocks.md", "w", encoding="utf-8").write(
        to_markdown(items, today, check, imoex, regime))

    hd = sorted({i.get("hist_last_date") for i in items
                 if i.get("hist_last_date")})
    if hd:
        print(f"  история в кэше до {hd[-1]}, цены из живой доски за {today}")
    moved = [i for i in items if abs(i.get("price_moved_since_hist") or 0) > 3]
    if moved:
        print(f"  сильно сдвинулись с последней свечи: "
              f"{', '.join(i['secid'] for i in moved[:8])}")
    print(f"Готово: {len(items)} бумаг")
    print(f"  топ: {', '.join(top)}")
    print(f"  дно: {', '.join(bottom)}")
    if check:
        print(f"  проверка прошлого прогноза: топ {check.get('top')}%, "
              f"дно {check.get('bottom')}%, индекс {check.get('imoex')}%")


if __name__ == "__main__":
    main()

