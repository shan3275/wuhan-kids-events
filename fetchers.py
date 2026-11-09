"""抓取层：官网直抓 + Bing RSS 搜索轮询 + 文章正文抓取。

三个抓取通道各有分工：
- 直抓列表页（湖北省图书馆楚天书房栏目）：最可靠、自带日期
- Bing RSS 关键词轮询：覆盖没有列表页的来源（公众号文章、新闻转载）
- 文章正文抓取：拿到标题链接后抓正文，供 AI 提取时间/年龄/报名等结构化信息
"""
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
MP_PATTERN = re.compile(r"^https?://mp\.weixin\.qq\.com/")


def get(url, timeout=20, retries=2, headers=None):
    """带重试的 GET，反爬页面（473/403）也会多试几次换角度。"""
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(1 + i * 2)
    print(f"[fetch] 失败 {url}: {last}")
    return None


def fetch_library_chutian(url):
    """湖北省图书馆·楚天书房活动预告列表页。

    结构：<a class="list-item-gd" title="标题"> 链接到公众号文章，
    所在行有 <div class="list-item-date"> 日期。
    """
    r = get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    items = []
    for a in soup.select("a.list-item-gd"):
        title = (a.get("title") or a.get_text(strip=True)).strip()
        href = a.get("href", "")
        if not title or not href:
            continue
        date = ""
        for parent in a.parents:
            date_el = parent.select_one(".list-item-date")
            if date_el:
                date = date_el.get_text(strip=True)
                break
        items.append({"title": title, "url": href, "date": date, "source": "楚天书房活动预告"})
    return items


def fetch_hbstm_schedule_index(url):
    """湖北省科技馆「活动日程」列表页（/webart/WebArtIdx/117/）。

    页面是服务端渲染的表格，不是 JS 加载。每一行两个相同链接：
    左边日期牌（日 + YY-MM），右边标题，链到当月公众号日程长图。
    """
    r = get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    items, seen = [], set()
    for tr in soup.select("tr.border_bottom"):
        link = tr.select_one("a[href*='mp.weixin.qq.com']")
        if not link:
            continue
        href = (link.get("href") or "").strip()
        if not href or href in seen:
            continue
        title_el = tr.select_one(".line_height5")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            anchors = tr.select("a[href*='mp.weixin.qq.com']")
            title = anchors[-1].get_text(strip=True) if anchors else ""
        day = tr.select_one(".h18")
        day = day.get_text(strip=True) if day else ""
        font = tr.find("font")
        ym = font.get_text(strip=True) if font else ""
        date = _hbstm_publish_date(day, ym)
        if not title:
            continue
        seen.add(href)
        items.append({
            "title": title,
            "url": href,
            "date": date,
            "source": "湖北省科技馆活动日程",
        })
    return items


def _hbstm_publish_date(day, ym):
    """日期牌：上排是日，下排是 YY-MM（如 03 / 26-11 → 2026-11-03）。"""
    m = re.fullmatch(r"(\d{2})-(\d{2})", (ym or "").strip())
    if not m or not (day or "").isdigit():
        return ""
    yy, mm = int(m.group(1)), int(m.group(2))
    if not 1 <= mm <= 12 and 1 <= yy <= 12:
        yy, mm = mm, yy
    if not 1 <= mm <= 12:
        return ""
    year = 2000 + yy if yy < 100 else yy
    dd = int(day)
    if not 1 <= dd <= 31:
        return ""
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def fetch_mp_article(url):
    """抓公众号文章：正文文本 + 图片地址（日程表画在长图里）。"""
    r = get(url)
    if not r:
        return {"title": "", "url": url, "text": "", "images": []}
    soup = BeautifulSoup(r.content, "html.parser")
    title_el = soup.select_one("#activity-name") or soup.find("title")
    body = soup.select_one("#js_content")
    title = title_el.get_text(strip=True) if title_el else ""
    images = []
    if body:
        for img in body.select("img"):
            src = (img.get("data-src") or img.get("src") or "").strip()
            if src.startswith("http"):
                images.append(src.split("#")[0])
        text = body.get_text("\n", strip=True)
    else:
        desc = soup.select_one('meta[property="og:description"]')
        text = desc["content"] if desc and desc.get("content") else ""
    return {"title": title, "url": url, "text": text, "images": images}


def fetch_hbstm_homepage(url):
    """湖北省科技馆官网首页。

    通知公告(?p=72)和活动日程(?p=117)会挂最新文章；
    当月课表以活动日程列表页为准（见 fetch_hbstm_schedule_index）。
    """
    r = get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    items = []
    for a in soup.select("a[href*='WebArtInfo']"):
        href = a.get("href", "")
        if "?p=72" not in href and "?p=117" not in href:
            continue
        title = a.get_text(strip=True)
        if title and len(title) > 4:
            items.append({"title": title, "url": urljoin(url, href), "date": "",
                          "source": "湖北省科技馆官网"})
    return items


def bing_search(query, limit=8):
    """Bing RSS 接口做关键词轮询，返回 [{title, url, date, snippet}]。"""
    r = get(f"https://www.bing.com/search?q={requests.utils.quote(query)}&format=rss&count={limit}")
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc = item.findtext("description") or ""
        snippet = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        items.append({
            "title": title,
            "url": link,
            "date": (item.findtext("pubDate") or "").strip(),
            "snippet": snippet[:300],
        })
    return items


def fetch_article(url, max_chars=2500):
    """抓文章正文：公众号文章用 #js_content，官网文章用通用降级。"""
    r = get(url)
    if not r:
        return {"title": "", "url": url, "text": ""}
    soup = BeautifulSoup(r.content, "html.parser")

    if MP_PATTERN.match(url):
        title_el = (soup.select_one("#activity-name") or soup.select_one("h1")
                    or soup.find("title"))
        body_el = soup.select_one("#js_content")
    else:
        title_el = soup.find("title")
        body_el = soup.select_one(".desc") or soup.select_one(".article-content") or soup.body

    title = title_el.get_text(strip=True) if title_el else ""
    if MP_PATTERN.match(url) and body_el is None:
        # 公众号文章偶尔结构不同，退回元信息
        desc = soup.select_one('meta[property="og:description"]')
        text = desc["content"] if desc else ""
    else:
        text = body_el.get_text("\n", strip=True) if body_el else ""

    return {"title": title, "url": url, "text": text[:max_chars]}
