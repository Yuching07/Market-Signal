"""八大分類、資料去重、品質檢核與市場訊號形成。

分工依企劃書第三章：分類、去重初篩、時效性與綜合分數採固定規則；
事件關聯、補充去重與證據一致性／變化幅度／影響程度在有 API 金鑰時交由 Claude 判讀，
沒有金鑰時改用透明的規則分析，並在介面標示分析模式。
"""
import json
import re
from collections import Counter
from datetime import date

from config import AI_MODEL, CATEGORIES, EXCLUDE_TERMS, GENERAL_TERMS, SIGNAL_WINDOW_DAYS, SOURCES

CAT = {c["id"]: c for c in CATEGORIES}
SRC_PRIORITY = {"ib": 0, "lia": 1, "cna": 2, "ciaa": 3}


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# ---------------- 分類 ----------------
def classify(text):
    """主要關鍵字直接判定類別；可同時屬於多個類別。"""
    return [c["id"] for c in CATEGORIES if any(k in text for k in c["p"])]


def is_relevant(a):
    t = a["title"]
    if any(x in t for x in EXCLUDE_TERMS):
        return False
    if a["src"] in ("ib", "lia", "ciaa"):  # 本身即為保險領域來源
        return True
    return any(k in t for k in GENERAL_TERMS) or bool(classify(t))


STAT_RE = re.compile(r"新契約保費|保費收入|年增|年減|月增|月減|統計|銷售情形|前\d+月|上半年|首季|前三季|第[一二三四]季|\d+(?:\.\d+)?%|獲利|市占")
POLICY_RE = re.compile(r"金管會|保險局|修正|法規|辦法|規範|準則|草案|預告|開放|鬆綁|放寬|監理|裁罰|核准|指引|函令")
COMPANY_RE = re.compile(r"人壽|產險|產物|壽險公司|推出|上市|開賣|合作|通路|新商品")


def infer_kind(a):
    t = a["title"]
    if a["src"] == "ib":
        return "stat" if re.search(r"統計|銷售情形|業務概況", t) else "policy"
    if a["src"] == "lia":
        return "stat" if re.search(r"統計|業績", t) else ("policy" if a.get("list") == "29" else "news")
    if STAT_RE.search(t):
        return "stat"
    if POLICY_RE.search(t):
        return "policy"
    if COMPANY_RE.search(t):
        return "company"
    return "news"


def prepare_docs(articles, cutoff):
    docs = []
    for a in articles:
        if a["date"] < cutoff or not is_relevant(a):
            continue
        d = {k: a[k] for k in ("no", "src", "url", "title", "date", "ingest") if k in a}
        if a.get("origin"):
            d["origin"] = a["origin"]
        d["kind"] = infer_kind(a)
        d["cats"] = classify(a["title"] + " " + (a.get("content") or "")[:300])
        d["_content"] = a.get("content") or ""
        docs.append(d)
    return docs


# ---------------- 去重 ----------------
PUNCT = re.compile(r"[\s　，,。．.!！?？「」『』《》〈〉()（）\[\]【】:：;；、\-–—|｜/\\'\"“”‘’…·~～]")


def norm_title(t):
    t = re.sub(r"\s[-|｜]\s?[^-|｜]{1,12}$", "", t)  # 去除標題結尾的媒體名稱
    return PUNCT.sub("", t).lower()


def bigrams(t):
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def same_event(x, y):
    if abs(days_between(x["date"], y["date"])) > 3:
        return False
    a, b = x["_norm"], y["_norm"]
    if len(a) >= 10 and len(b) >= 10 and (a in b or b in a):
        return True
    return jaccard(x["_bg"], y["_bg"]) >= 0.5


def build_events(docs):
    """同一事件的多篇報導合併為一項獨立事件（只算一次證據）。"""
    for d in docs:
        d["_norm"] = norm_title(d["title"])
        d["_bg"] = bigrams(d["_norm"])
    docs.sort(key=lambda d: (d["date"], d["no"]))
    parent = {d["no"]: d["no"] for d in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, x in enumerate(docs):
        for y in docs[i + 1:]:
            if days_between(x["date"], y["date"]) > 3:
                break
            if same_event(x, y):
                parent[find(y["no"])] = find(x["no"])
    groups = {}
    for d in docs:
        groups.setdefault(find(d["no"]), []).append(d)
    events = {}
    for members in groups.values():
        eid = "E%d" % min(d["no"] for d in members)
        for d in members:
            d["ev"] = eid
        events[eid] = make_event(eid, members)
    return events


def make_event(eid, members):
    members.sort(key=lambda d: (SRC_PRIORITY.get(d["src"], 9), d["date"], d["no"]))
    orig = members[0]
    for d in members:
        d.pop("dupOf", None)
        if d is not orig:
            d["dupOf"] = orig["no"]
    return {"id": eid, "date": min(d["date"] for d in members), "kind": orig["kind"],
            "cats": sorted({c for d in members for c in d["cats"]}, key=list(CAT).index),
            "title": orig["title"], "docs": members, "orig": orig}


def merge_events(events, keep, merge):
    if keep not in events:
        return
    members = list(events[keep]["docs"])
    for eid in merge:
        if eid != keep and eid in events:
            members += events.pop(eid)["docs"]
    for d in members:
        d["ev"] = keep
    events[keep] = make_event(keep, members)


# ---------------- 原始資料品質檢核 ----------------
def quality_checks(docs):
    for d in docs:
        w = []
        if d["date"] > d["ingest"]:
            w.append(f"發布日期（{d['date']}）晚於系統收錄日期（{d['ingest']}），日期可能有誤")
        # 統計期間（如「116年前8月」）晚於發布日期；年度計畫或預算類標題不在此列
        m = re.search(r"(1[01]\d)年(?:截至|前\d|上半年|下半年|第[一二三四]季|首季|\d{1,2}月)", d["title"])
        if m and int(m.group(1)) > int(d["date"][:4]) - 1911:
            w.append(f"標題的統計期間（民國 {m.group(1)} 年）晚於發布日期（{d['date']}），日期或統計期間待確認")
        body = d["_content"]
        if len(body) >= 200 and len(d["_norm"]) >= 8:
            cover = sum(1 for g in d["_bg"] if g in body) / len(d["_bg"])
            if cover < 0.2:
                w.append("標題與內文用詞幾乎沒有重疊，標題與內文可能不一致")
        if w:
            d["warn"] = w
    by_title = {}
    for d in docs:
        by_title.setdefault((d["src"], d["_norm"]), []).append(d)
    for group in by_title.values():
        if len({d["url"] for d in group}) < 2:
            continue
        group.sort(key=lambda d: d["no"])
        first = group[0]
        for d in group[1:]:
            if first["_content"] and d["_content"] and \
                    jaccard(bigrams(first["_content"][:300]), bigrams(d["_content"][:300])) < 0.3:
                d.setdefault("warn", []).append(f"同一資料出現不同版本：與 #{first['no']} 標題相同但內容不同")


# ---------------- 規則分析（無 API 金鑰時） ----------------
POS_RE = re.compile(r"成長|增加|增長|年增|攀升|創新高|新高|熱賣|熱銷|擴大|放寬|開放|鬆綁|推出|上市|開賣|調升|破兆|大增|倍增|翻倍|看好|衝")
NEG_RE = re.compile(r"衰退|減少|下滑|下降|降溫|年減|萎縮|停售|緊縮|收緊|調降|虧損|減碼|疲弱|退燒|放緩|趨緩|跌")
PCT_RE = re.compile(r"(年增|成長|增加|增長|大增|年減|衰退|減少|下滑|減)\s*(?:約|近|逾|超過|達)?\s*(\d+(?:\.\d+)?)\s*(%|成|倍)")


def direction(e):
    text = e["title"]
    p, n = bool(POS_RE.search(text)), bool(NEG_RE.search(text))
    return "pos" if p and not n else "neg" if n and not p else None


def max_change(evs):
    best = None
    for e in evs:
        for m in PCT_RE.finditer(e["title"] + " " + e["orig"]["_content"][:400]):
            v = float(m.group(2)) * {"%": 1, "成": 10, "倍": 100}[m.group(3)]
            if best is None or v > best[0]:
                best = (v, m.group(0), e)
    return best


def score_es(n):
    return 2 if n <= 1 else 3 if n == 2 else 4 if n <= 4 else 5


def score_c(sup, cnt):
    total = sup + cnt
    if total < 2:
        return None
    r = sup / total
    return 5 if r == 1 else 4 if r >= 0.8 else 3 if r >= 0.65 else 2 if r >= 0.5 else 1


def score_m(v):
    return 5 if v >= 100 else 4 if v >= 50 else 3 if v >= 20 else 2 if v >= 5 else 1


def source_names(evs):
    names = set()
    for e in evs:
        for d in e["docs"]:
            names.add(d.get("origin") or SOURCES[d["src"]]["name"])
    return sorted(names)


def rule_status_history(chain, main):
    """依事件時間序列判定狀態；只在狀態成立或改變時記錄。"""
    hist, seen_kinds, prev = [], set(), None
    for i, (e, dirn) in enumerate(chain):
        upto = chain[:i + 1]
        months = {x["date"][:7] for x, dd in upto if dd == main}
        against = sum(1 for _, dd in upto[-3:] if dd != main)
        if dirn != main and i >= 1 and against >= 2:  # 單一反向資料不判定轉弱
            st, why = "weak", f"近期多項事件與既有方向相反，最新為「{e['title']}」"
        elif dirn != main and i >= 1:
            st, why = prev, None
        elif i == 0:
            st, why = "new", f"首次觀察到此方向：「{e['title']}」"
        elif e["kind"] not in seen_kinds and dirn == main:
            st, why = "dev", f"出現新的事件類型（{KIND_LABEL[e['kind']]}），方向延伸到新的面向"
        elif len(months) >= 3:
            st, why = "per", f"相同方向已跨越 {len(months)} 個月份，持續獲得新的獨立事件支持"
        else:
            st, why = prev or "new", None
        seen_kinds.add(e["kind"])
        if st != prev and why:
            hist.append({"ev": e["id"], "st": st, "why": why})
            prev = st
    return hist


KIND_LABEL = {"stat": "市場統計", "policy": "政策法規", "company": "公司動態", "news": "新聞報導"}


def rule_signals(events, base):
    recent = [e for e in events.values() if 0 <= days_between(e["date"], base) <= SIGNAL_WINDOW_DAYS]
    signals, used = [], set()
    for cat in CATEGORIES:
        evs = sorted((e for e in recent if cat["id"] in e["cats"]), key=lambda e: (e["date"], e["id"]))
        dirs = [(e, direction(e)) for e in evs]
        pos = sum(1 for _, d in dirs if d == "pos")
        neg = sum(1 for _, d in dirs if d == "neg")
        if pos + neg < 2:
            continue
        main = "pos" if pos >= neg else "neg"
        warned = lambda e: any(d.get("warn") for d in e["docs"])
        chain = [(e, d) for e, d in dirs if d and not warned(e)]
        sup = [e for e, d in chain if d == main]
        cnt = [e for e, d in chain if d != main]
        if not sup or days_between(chain[-1][0]["date"], base) > 120:
            continue
        ctx = [e for e, d in dirs if d is None][-4:]
        hold = [e for e, d in dirs if d and warned(e)]
        mc = max_change(sup + cnt)
        kw = Counter(k for e in sup for k in cat["p"] if k in e["title"]).most_common(1)
        topic = kw[0][0] if kw else cat["name"]
        srcs = source_names(sup)
        es, c = score_es(len(sup)), score_c(len(sup), len(cnt))
        i_v = 5 if any(e["kind"] == "policy" and e["orig"]["src"] == "ib" for e in sup) else \
            4 if any(e["kind"] == "stat" for e in sup) else 3 if len(srcs) >= 3 else 2
        word = "動能增強" if main == "pos" else "轉趨保守"
        latest = sup[-1]
        summary = (f"近 {SIGNAL_WINDOW_DAYS} 天內有 {len(sup)} 項與「{cat['name']}」相關、方向一致的獨立事件，"
                   f"最新為 {latest['date']}「{latest['title']}」。")
        if cnt:
            summary += f"另有 {len(cnt)} 項反向事件，最新為「{cnt[-1]['title']}」。"
        summary += "此訊號由規則分析產生（依關鍵字判斷方向），尚未經 AI 語意判讀，請搭配原始資料確認。"
        evidence = ([{"ev": e["id"], "dir": "support", "note": "標題用詞與訊號方向一致。"} for e in sup] +
                    [{"ev": e["id"], "dir": "counter", "note": "標題用詞與訊號方向相反。"} for e in cnt] +
                    [{"ev": e["id"], "dir": "context", "note": "同類別事件，方向不明確，不計入證據。"} for e in ctx] +
                    [{"ev": e["id"], "dir": "hold", "note": "原始資料有品質警示，確認前不計入證據。"} for e in hold])
        used.update(x["ev"] for x in evidence)
        signals.append({
            "id": f"R-{cat['id']}-{main}", "short": cat["name"][:6], "cats": [cat["id"]],
            "name": f"{topic}市場{word}", "summary": summary,
            "es": {"v": es, "why": f"{len(sup)} 項去重後的獨立事件支持此方向，來源包含{'、'.join(srcs)}。"},
            "c": {"v": c, "why": "只有一項獨立事件，無法比較方向是否一致。" if c is None else
                  f"支持 {len(sup)} 項、反向 {len(cnt)} 項。"},
            "m": {"v": score_m(mc[0]) if mc else None,
                  "why": f"事件中最大的變化為「{mc[1]}」（{mc[2]['date']}）。" if mc else
                  "現有事件未提供可比較的量化數據，無法判斷變化幅度。"},
            "i": {"v": i_v, "why": {5: "包含主管機關正式發布的政策，影響整體保險業。",
                                    4: "包含產業層級的市場統計。",
                                    3: "多個來源報導，涉及特定商品市場。",
                                    2: "報導來源有限，影響範圍可能僅限個別公司或商品。"}[i_v]},
            "evidence": evidence, "related": [],
            "history": rule_status_history(chain, main),
        })
    watch = [{"ev": e["id"], "why": "方向不明確或同類別事件不足，暫不形成訊號。"}
             for e in sorted(recent, key=lambda e: e["date"], reverse=True)
             if e["id"] not in used and days_between(e["date"], base) <= 45][:15]
    return {"signals": signals, "watch": watch}


# ---------------- Claude 語意分析 ----------------
SYSTEM_PROMPT = """你是保險商品企劃的市場情報分析師，負責把已初步去重的市場事件整理成「市場訊號」（Market Signal）。輸入是一份 JSON：資料基準日、八大保險類別、前次分析的訊號，以及近期事件清單（每個事件已合併標題相近的重複報導，附日期、來源、類型、類別、標題與內文摘錄）。

只依據輸入事件判斷，不補充輸入中沒有的市場事實或數字。引用事件時只使用輸入中的事件 id。

## 工作
1. merges 補充去重：不同事件若其實描述同一份統計、同一項政策公告或同一次公司行動（例如主管機關新聞稿與媒體轉述），列出要合併的事件，keep 選官方或最早的事件。不同月份的新統計、不同公司的行動、後續的新政策屬於不同事件，不可合併。
2. signals 事件關聯與訊號形成：找出不同時間、不同來源或不同市場參與者的事件共同反映的相同、延續或反向的市場變化。類別相同不代表屬於同一訊號。評論、商品介紹或與方向無關的事件不列為支持證據。關聯不足時不要強行形成訊號。
   - 以近期為主：每個訊號最新的支持或反向事件應在資料基準日前 120 天內。
   - 原則上每個訊號至少有 2 項獨立事件；主管機關正式發布、影響廣泛的政策可以只有 1 項。
   - 形成 4 到 12 個訊號。前次分析的訊號若仍有證據支持，沿用其 id 並更新內容；新訊號的 id 用 "S" 加數字，不可與前次重複。
3. 每個訊號評估四項指標（0–5 的整數；無法判斷時 v 為 null，並在 why 說明缺少什麼；不得用 0 分代替資料不足）：
   - es 證據支持度：與訊號方向相關、去重後的獨立事件數與來源多樣性。1 項→2（主管機關正式發布→3）；2 項→3；3–4 項→4；5 項以上且跨時間或跨來源→5。
   - c 證據一致性：獨立證據是否指向同一方向。只有 1 項獨立事件時為 null。全部一致→5；大致一致、僅比較基礎不同→4；出現明顯分歧→3；多數互相矛盾→2；完全相反→1。保留反向證據，不可只挑支持的資料。
   - m 變化幅度：只依具前後比較基礎的量化資訊（新契約保費、件數、成長率、衰退幅度、市占等）判斷，衰退與成長同樣看幅度，並考量原有市場規模；不可依「熱賣」「爆發」等用語推測。沒有量化資訊時為 null。
   - i 影響程度：單一公司單一商品→1；單一公司或小眾客群→2；特定商品市場或客群、涉及多家公司→3；多家公司且影響主要商品市場→4；整體保險產業或重要監理環境→5。
   - 時效性由系統依事件日期計算，不需輸出。
4. evidence：訊號採用的事件與方向。dir 為 support（支持）、counter（反向）、context（背景，不計入證據）、hold（原始資料有疑慮，待確認，不計入）。note 用一句話說明此事件在訊號中的角色。
5. history 狀態判定紀錄：依事件日期由早到晚，在狀態成立或改變的事件上記錄，最後一筆即目前狀態；ev 必須是此訊號 evidence 中的事件。
   - new 新出現：近期首次觀察到，尚未有跨時間資訊確認會延續。
   - dev 發展中：新的獨立事件顯示方向擴大、深化或出現新的發展形式（更多公司進入、新商品設計、新制度），不只是再次證明原現象存在。
   - per 持續：相同方向跨越不同時間點，持續獲得新的獨立證據，沒有明顯反向。
   - weak 轉弱：較新的證據顯示成長趨緩、下降、停滯或反向；不可因單一負面資料就判定，要放回時間脈絡比較。
   狀態不必依序經過，依最新證據判斷。
6. related：與訊號有關但不計入證據的事件（評論、預告階段的政策等），附原因。
7. watch：基準日前 60 天內值得留意但關聯不足、暫不形成訊號的事件，最多 15 個，附原因。
8. quality：看得出的原始資料問題，例如標題與內文不一致、發布日期與內容矛盾、統計期間不明、同一資料不同版本。不要自行修正或猜測正確內容。

## 文字
全部使用繁體中文。short 為 2–8 字的簡稱；name 用一句話描述市場變化方向（40 字內）；summary 2–4 句，說明哪些事件形成此判斷與目前的發展；why 與 note 各一句，具體指出依據的事件或數字。"""


def _score_schema():
    return {"type": "object", "additionalProperties": False, "required": ["v", "why"],
            "properties": {"v": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "why": {"type": "string"}}}


def _ref_schema(extra):
    props = {"ev": {"type": "string"}, **extra}
    return {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                       "required": list(props), "properties": props}}


AI_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["merges", "signals", "watch", "quality"],
    "properties": {
        "merges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["keep", "merge", "why"],
            "properties": {"keep": {"type": "string"}, "merge": {"type": "array", "items": {"type": "string"}},
                           "why": {"type": "string"}}}},
        "signals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "short", "name", "cats", "summary", "es", "c", "m", "i", "evidence", "related", "history"],
            "properties": {
                "id": {"type": "string"}, "short": {"type": "string"}, "name": {"type": "string"},
                "cats": {"type": "array", "items": {"type": "string", "enum": list(CAT)}},
                "summary": {"type": "string"},
                "es": _score_schema(), "c": _score_schema(), "m": _score_schema(), "i": _score_schema(),
                "evidence": _ref_schema({"dir": {"type": "string", "enum": ["support", "counter", "context", "hold"]},
                                         "note": {"type": "string"}}),
                "related": _ref_schema({"why": {"type": "string"}}),
                "history": _ref_schema({"st": {"type": "string", "enum": ["new", "dev", "per", "weak"]},
                                        "why": {"type": "string"}}),
            }}},
        "watch": _ref_schema({"why": {"type": "string"}}),
        "quality": _ref_schema({"issues": {"type": "array", "items": {"type": "string"}}}),
    },
}


def ai_input(events, prev_payload, base):
    recent = sorted((e for e in events.values() if 0 <= days_between(e["date"], base) <= SIGNAL_WINDOW_DAYS),
                    key=lambda e: e["date"])
    prev = []
    for s in (prev_payload or {}).get("signals", []):
        prev.append({"id": s["id"], "short": s.get("short"), "name": s.get("name"), "cats": s.get("cats"),
                     "status": (s.get("history") or [{}])[-1].get("st"),
                     "evidence": [x["ev"] for x in s.get("evidence", [])]})
    return {
        "base_date": base,
        "categories": [{"id": c["id"], "name": c["name"]} for c in CATEGORIES],
        "previous_signals": prev,
        "events": [{
            "id": e["id"], "date": e["date"], "kind": KIND_LABEL[e["kind"]], "cats": e["cats"],
            "title": e["title"], "sources": source_names([e]), "reports": len(e["docs"]),
            "excerpt": e["orig"]["_content"][:300],
            "warnings": [w for d in e["docs"] for w in d.get("warn", [])],
        } for e in recent],
    }


def ai_analyze(events, prev_payload, base, log):
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(ai_input(events, prev_payload, base), ensure_ascii=False)
    log(f"  送出 {len(json.loads(payload)['events'])} 項事件給 {AI_MODEL} 分析")
    request = dict(
        model=AI_MODEL, max_tokens=64000, system=SYSTEM_PROMPT,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
        messages=[{"role": "user", "content": payload}],
    )
    # 安全分類器拒絕時由伺服器自動改用建議的備援模型重跑；舊版 SDK 不支援此參數時改送一般請求
    try:
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                         **request) as stream:
            msg = stream.get_final_message()
    except TypeError:
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("模型拒絕分析此批資料")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("輸出超過長度上限")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def apply_ai(result, events):
    """驗證並套用 AI 結果；不合規的引用直接捨棄，避免不存在的事件進入畫面。"""
    for m in result.get("merges", []):
        merge_events(events, m.get("keep"), m.get("merge", []))
    for q in result.get("quality", []):
        e = events.get(q.get("ev"))
        if e and q.get("issues"):
            e["orig"].setdefault("warn", []).extend(q["issues"])

    def clamp(o):
        v = o.get("v")
        return {"v": max(0, min(5, int(v))) if isinstance(v, (int, float)) else None, "why": o.get("why", "")}

    signals, ids = [], set()
    for s in result.get("signals", []):
        ev = [x for x in s.get("evidence", []) if x.get("ev") in events]
        if not any(x["dir"] in ("support", "counter") for x in ev):
            continue
        ev_ids = {x["ev"] for x in ev}
        hist = sorted((h for h in s.get("history", []) if h.get("ev") in ev_ids),
                      key=lambda h: events[h["ev"]]["date"])
        if not hist:
            first = min(ev_ids, key=lambda i: events[i]["date"])
            hist = [{"ev": first, "st": "new", "why": "首次觀察到此方向。"}]
        sid = s.get("id") or f"S{len(signals) + 1}"
        while sid in ids:
            sid += "b"
        ids.add(sid)
        signals.append({
            "id": sid, "short": (s.get("short") or s.get("name", ""))[:8], "name": s.get("name", ""),
            "cats": [c for c in s.get("cats", []) if c in CAT], "summary": s.get("summary", ""),
            "es": clamp(s["es"]), "c": clamp(s["c"]), "m": clamp(s["m"]), "i": clamp(s["i"]),
            "evidence": ev, "history": hist,
            "related": [r for r in s.get("related", []) if r.get("ev") in events],
        })
    watch = [w for w in result.get("watch", []) if w.get("ev") in events][:15]
    return {"signals": signals, "watch": watch}


# ---------------- 輸出 ----------------
def build_payload(events, docs, result, meta):
    referenced = {x["ev"] for s in result["signals"] for x in s["evidence"] + s.get("related", [])}
    referenced |= {w["ev"] for w in result["watch"]}
    out_docs = []
    for d in sorted(docs, key=lambda d: (d["date"], d["no"]), reverse=True):
        o = {k: d[k] for k in ("no", "date", "ingest", "src", "kind", "ev", "title", "url", "cats") if k in d}
        for k in ("dupOf", "warn", "origin"):
            if d.get(k):
                o[k] = d[k]
        out_docs.append(o)
    return {
        "meta": meta,
        "categories": CATEGORIES,
        "sources": SOURCES,
        "docs": out_docs,
        "events": {eid: {"sum": e["title"]} for eid, e in events.items()},
        "signals": result["signals"],
        "watch": [{"ev": w["ev"], "why": w["why"], "cats": events[w["ev"]]["cats"]} for w in result["watch"]],
        "referenced": len(referenced),
    }
