"""市場情報雷達：每日更新資料。

用法：
  python pipeline/update.py              抓取新資料並重新分析
  python pipeline/update.py --no-fetch   只用已收錄的資料重新分析
  python pipeline/update.py --no-ai      不呼叫 Claude，改用規則分析

設定環境變數 ANTHROPIC_API_KEY 時使用 Claude 語意分析，否則自動改用規則分析。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze as A  # noqa: E402
import sources as S  # noqa: E402
from config import AI_MODEL, LOOKBACK_DAYS, MAX_DETAIL_FETCH, MAX_LIST_PAGES, SIGNAL_WINDOW_DAYS, TZ  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STORE = os.path.join(DATA, "articles.json")
OUT = os.path.join(DATA, "signals.json")


def log(msg):
    print(msg, flush=True)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def save(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    os.replace(tmp, path)


def collect(store, cutoff, today, max_pages):
    by_key = {a["key"]: a for a in store["articles"]}
    f = S.Fetcher()
    jobs = [
        ("ib", lambda: S.fetch_ib(f, cutoff, max_pages, log)),
        ("cna", lambda: S.fetch_cna(f, cutoff, log)),
        ("lia", lambda: S.fetch_lia(f, cutoff, log)),
        ("ciaa", lambda: S.fetch_ciaa(f, cutoff, max_pages, log)),
    ]
    status = {}
    for src, job in jobs:
        try:
            items = job()
        except Exception as e:  # 單一來源失敗不影響其他來源，既有資料保留
            status[src] = {"ok": False, "error": str(e)[:200], "checked": today}
            log(f"  ✗ {src} 抓取失敗：{e}")
            continue
        new = 0
        for it in items:
            if it["key"] in by_key:
                continue
            it["no"] = store["next_no"]
            store["next_no"] += 1
            it["ingest"] = today
            store["articles"].append(it)
            by_key[it["key"]] = it
            new += 1
        status[src] = {"ok": True, "fetched": len(items), "new": new, "checked": today}

    # 新資料才抓內文（金管會、中央社），供分類、去重與 AI 判讀
    budget, ib_down = MAX_DETAIL_FETCH, not status.get("ib", {}).get("ok")
    for a in sorted(store["articles"], key=lambda a: a["date"], reverse=True):
        if budget <= 0:
            break
        if a["src"] not in ("ib", "cna") or a.get("content") is not None or a["date"] < cutoff:
            continue
        if not A.is_relevant(a) or (a["src"] == "ib" and ib_down):
            continue
        budget -= 1
        try:
            if a["src"] == "ib":
                a["content"] = S.ib_detail(f, a["url"])[:800]
            else:
                body, pub = S.cna_article(f, a["url"])
                a["content"] = body[:800]
                if pub:
                    a["date"] = pub
        except S.SourceUnavailable:
            ib_down = True
        except Exception as e:
            a["fetch_errors"] = a.get("fetch_errors", 0) + 1
            if a["fetch_errors"] >= 3:
                a["content"] = ""  # 連續失敗三次後不再重試
            log(f"  內文抓取失敗 #{a['no']}：{e}")
    log(f"  抓取內文 {MAX_DETAIL_FETCH - budget} 篇")
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--max-pages", type=int, default=MAX_LIST_PAGES)
    args = ap.parse_args()

    now = datetime.now(TZ)
    today = now.date().isoformat()
    cutoff = (now.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    store = load(STORE, {"next_no": 1, "articles": []})
    prev = load(OUT, None)
    prev_meta = (prev or {}).get("meta", {})

    if args.no_fetch:
        status = prev_meta.get("sources_status", {})
    else:
        log("蒐集資料…")
        status = collect(store, cutoff, today, args.max_pages)
        keep_from = (now.date() - timedelta(days=LOOKBACK_DAYS * 2)).isoformat()
        store["articles"] = [a for a in store["articles"] if a["date"] >= keep_from]
        save(STORE, store)

    log("分類、去重與品質檢核…")
    docs = A.prepare_docs(store["articles"], cutoff)
    events = A.build_events(docs)
    A.quality_checks(docs)
    log(f"  相關資料 {len(docs)} 筆，去重後 {len(events)} 項事件")

    mode, ai_error = "rules", None
    result = None
    if not args.no_ai and os.environ.get("ANTHROPIC_API_KEY"):
        log("Claude 語意分析…")
        try:
            result = A.apply_ai(A.ai_analyze(events, prev, today, log), events)
            mode = "ai"
        except Exception as e:
            ai_error = f"{type(e).__name__}: {e}"[:300]
            log(f"  AI 分析失敗，改用規則分析：{ai_error}")
    if result is None:
        log("規則分析…")
        result = A.rule_signals(events, today)
    log(f"  形成 {len(result['signals'])} 個市場訊號，觀察中 {len(result['watch'])} 項")

    prev_base = prev_meta.get("base")
    meta = {
        "generated": now.isoformat(timespec="minutes"),
        "base": today,
        "prev": prev_base if prev_base and prev_base != today else prev_meta.get("prev"),
        "mode": mode, "model": AI_MODEL if mode == "ai" else None, "ai_error": ai_error,
        "lookback_days": LOOKBACK_DAYS, "window_days": SIGNAL_WINDOW_DAYS,
        "sources_status": status, "doc_count": len(docs), "event_count": len(events),
    }
    save(OUT, A.build_payload(events, docs, result, meta))
    log(f"完成：{OUT}")


if __name__ == "__main__":
    main()
