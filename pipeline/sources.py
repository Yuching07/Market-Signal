"""各公開資料來源的抓取程式。每個函式回傳 dict 清單：
{src, url, title, date(YYYY-MM-DD), content?, origin?, list?}
"""
import html
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

from config import (CNA_QUERIES, IB_LISTS, LIA_CATEGORIES, ORIGIN_DOMAINS, REQUEST_DELAY,
                    USER_AGENT)


class Fetcher:
    """帶節流與重試的簡易 HTTP 客戶端（同一網站請求之間至少間隔 REQUEST_DELAY 秒）。"""

    def __init__(self, delay=REQUEST_DELAY):
        self.delay = delay
        self.last = {}
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, data=None, timeout=30):
        host = urllib.parse.urlsplit(url).netloc
        wait = self.delay - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"})
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    raw = r.read()
                    charset = r.headers.get_content_charset() or "utf-8"
                self.last[host] = time.time()
                return raw.decode(charset, errors="replace")
            except Exception:
                self.last[host] = time.time()
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))


def clean_text(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def origin_name(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    for domain, name in ORIGIN_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return name
    return host.removeprefix("www.") or None


# ---------------- 金管會保險局 ----------------
IB_BASE = "https://www.ib.gov.tw/ch/"


class SourceUnavailable(Exception):
    pass


def _check_ib(s):
    if "Maintenance Page" in s or "網站維護" in s[:3000]:
        raise SourceUnavailable("金管會網站維護中")
    return s


def ib_list_page(f, lid, path, page):
    return parse_ib_list(_check_ib(f.get(f"{IB_BASE}home.jsp?id={lid}&parentpath={path}&page={page}")), lid)


def parse_ib_list(s, lid):
    anchors =[(m.start(), m) for m in re.finditer(
        r'<a href="(home\.jsp\?id=%s[^"]*?dataserno=(\d+)[^"]*)" title="([^"]*)"' % lid, s)]
    dates = [(m.start(), m.group(1)) for m in re.finditer(r'class="pdate1">\s*(\d{4}-\d{2}-\d{2})', s)]
    items = []
    for pos, m in anchors:
        d = next((dv for dp, dv in dates if dp > pos), None)
        if not d:
            continue
        href = html.unescape(m.group(1))
        items.append({"src": "ib", "url": IB_BASE + href, "title": html.unescape(m.group(3)).strip(),
                      "date": d, "key": "ib:" + m.group(2), "list": lid})
    return items


def ib_detail(f, url):
    return parse_ib_detail(_check_ib(f.get(url)))


def parse_ib_detail(s):
    i = s.find('class="contentdate"')
    if i < 0:
        return ""
    j = s.find("<!-- 相關連結 -->", i)
    seg = s[i:j if j > 0 else i + 20000]
    seg = re.sub(r'^class="contentdate">[^<]*</div>', "", seg)
    return clean_text("<" + seg)[:2000]


def fetch_ib(f, cutoff, max_pages, log):
    out, seen = [], set()
    for lid, path in IB_LISTS:
        for page in range(1, max_pages + 1):
            items = ib_list_page(f, lid, path, page)
            if not items:
                break
            for it in items:
                if it["key"] not in seen:
                    seen.add(it["key"])
                    out.append(it)
            if min(it["date"] for it in items) < cutoff:
                break
        log(f"  金管會保險局 列表 id={lid}：累計 {len(out)} 筆")
    return [it for it in out if it["date"] >= cutoff]


# ---------------- 中央社 ----------------
def _ld_json(s):
    for m in re.finditer(r"<script type='application/ld\+json'>(.*?)</script>", s, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        yield from (d if isinstance(d, list) else [d])


def cna_search(f, q):
    s = f.get("https://www.cna.com.tw/search/hysearchws.aspx?q=" + urllib.parse.quote(q))
    items = []
    for d in _ld_json(s):
        if d.get("@type") != "ItemList":
            continue
        for el in d.get("itemListElement", []):
            url, name = el.get("url", ""), el.get("name", "")
            m = re.search(r"/news/\w+/(\d{4})(\d{2})(\d{2})\d+\.aspx", url)
            if m and name:
                items.append({"src": "cna", "url": url, "title": html.unescape(name).strip(),
                              "date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "key": "cna:" + url})
    return items


def cna_article(f, url):
    s = f.get(url)
    for d in _ld_json(s):
        if d.get("@type") == "NewsArticle":
            body = d.get("articleBody") or d.get("about") or ""
            pub = (d.get("datePublished") or "")[:10]
            return clean_text(body)[:2000], pub
    return "", ""


def fetch_cna(f, cutoff, log):
    out, seen = [], set()
    for q in CNA_QUERIES:
        try:
            items = cna_search(f, q)
        except Exception as e:  # 單一關鍵字失敗不影響其他關鍵字
            log(f"  中央社 搜尋「{q}」失敗：{e}")
            continue
        for it in items:
            if it["key"] not in seen and it["date"] >= cutoff:
                seen.add(it["key"])
                out.append(it)
    log(f"  中央社 站內搜尋 {len(CNA_QUERIES)} 個關鍵字：{len(out)} 筆")
    return out


# ---------------- 壽險公會 ----------------
def fetch_lia(f, cutoff, log):
    out = []
    for catg in LIA_CATEGORIES:
        s = f.get(f"https://www.lia-roc.org.tw?catg_article_type=list&catg_id={catg}")
        for m in re.finditer(r'<li class="list-item">\s*<a href="\s*([^"]+?)\s*"[^>]*>.*?'
                             r'<h3 class="list-title[^"]*">(.*?)</h3>\s*<p class="list-date">(\d{4}-\d{2}-\d{2})</p>',
                             s, re.S):
            url = html.unescape(m.group(1)).strip()
            out.append({"src": "lia", "url": url, "title": clean_text(m.group(2)), "date": m.group(3),
                        "key": "lia:" + url, "list": catg})
    log(f"  壽險公會 最新消息／重要政策：{len(out)} 筆")
    return [it for it in out if it["date"] >= cutoff]


# ---------------- 保險代理人公會：各大網站新聞彙整 ----------------
CIAA_URL = "https://www.ciaa.org.tw/Announcement.aspx?PT=N"


def _ciaa_rows(s):
    rows = []
    for m in re.finditer(r'GridView1_hl_\d+"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>.*?<td>(\d{4})/(\d{1,2})/(\d{1,2})</td>',
                         s, re.S):
        url = html.unescape(m.group(1)).strip()
        rows.append({"src": "ciaa", "url": url, "title": clean_text(m.group(2)),
                     "date": f"{m.group(3)}-{int(m.group(4)):02d}-{int(m.group(5)):02d}",
                     "key": "ciaa:" + url, "origin": origin_name(url)})
    return rows


def _hidden_fields(s):
    return {m.group(1): html.unescape(m.group(2)) for m in re.finditer(
        r'<input type="hidden" name="(__[A-Z]+)" id="__[A-Z]+" value="([^"]*)"', s)}


def fetch_ciaa(f, cutoff, max_pages, log):
    s = f.get(CIAA_URL)
    out, page = [], 1
    while True:
        rows = _ciaa_rows(s)
        out.extend(rows)
        if not rows or page >= max_pages or min(r["date"] for r in rows) < cutoff:
            break
        nxt = f"Page${page + 1}"
        if nxt not in html.unescape(s):
            break
        data = _hidden_fields(s)
        data.update({"__EVENTTARGET": "ctl00$ContentPlaceHolder2$GridView1", "__EVENTARGUMENT": nxt})
        s = f.get(CIAA_URL, data=data)
        page += 1
    log(f"  保險代理人公會 新聞彙整 {page} 頁：{len(out)} 筆")
    return [it for it in out if it["date"] >= cutoff]
