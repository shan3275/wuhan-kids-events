"""湖北省科技馆当月活动日程。

活动日程列表页给出每月一篇公众号长图。场次、时间、地点画在图片里，
正文几乎没有日期，所以这里会：

1. 打开 /webart/WebArtIdx/117/ ，挑出覆盖「当前月份」的日程；
2. 识别长图里的课表；
3. 按预约方式分成三类，供到点提醒：
   - monday_noon：每周一 12:00 线上抢当周课程（工作室 / 趣探光影 / 活字讲坛）
   - onsite_9am：活动当天 9:00 现场取票（炫酷实验室 / 一楼大厅、科普报告厅表演）
   - walkin：展厅门口短实验，不用预约
"""
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import fetchers

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "state" / "hbstm.json"
CACHE = BASE / ".cache" / "hbstm"
TZ = ZoneInfo("Asia/Shanghai")

# 海报标题里出现这些词时，锁定所属系列和默认预约方式。靠前的词优先（标题通常在最前）。
SERIES_RULES = [
    ("活字讲坛", "both"),
    ("炫酷实验室", "onsite_9am"),
    ("科普联创", "none"),
    ("趣探研学", "none"),
    ("科学小百科", "walkin"),
    ("科学岛", "walkin"),
    ("趣探光影", "monday_noon"),
    ("虚拟探秘", "monday_noon"),
    ("天问课堂", "monday_noon"),
    ("电影技术", "monday_noon"),
    ("小手翻世界", "monday_noon"),
    ("科学工作室", "monday_noon"),
    ("好奇屋", "monday_noon"),
    ("发现屋", "monday_noon"),
    ("机智屋", "monday_noon"),
    ("智造工坊", "monday_noon"),
    ("坐看星河", "monday_noon"),
    ("山水野趣", "monday_noon"),
    ("科技模型", "monday_noon"),
]

DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
TIME_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～—–到至]+\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)
AGE_RE = re.compile(
    r"(\d+)\s*[-~～到至]\s*(\d+)\s*岁|(\d+)\s*岁\s*(及以上|以上|及以下|以下)"
)
PLACE_RE = re.compile(
    r"(一楼大厅|二楼大厅|科普报告厅|南门入口|B厅门口|C厅门口|"
    r"巨幕影院|球幕影院|沉浸影院|科学坊|影工坊|天问课堂|"
    r"[\u4e00-\u9fffA-Za-z0-9]{1,8}(?:门口|大厅|展厅|影院|活动室))"
)
HEADER_WORDS = {
    "日期", "时间", "活动名称", "主题内容", "适合年龄", "活动地点",
    "讲座主题", "讲座人", "活动时长", "第一场", "第二场", "活动安排",
}
CN_MONTH = {
    "十二": 12, "十一": 11, "十": 10, "九": 9, "八": 8, "七": 7, "六": 6,
    "五": 5, "四": 4, "三": 3, "二": 2, "一": 1, "正": 1,
}


def now_cn():
    return datetime.now(TZ)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _month_num(token):
    if token.isdigit():
        n = int(token)
        return n if 1 <= n <= 12 else None
    return CN_MONTH.get(token)


def article_span(title, published):
    """这篇日程覆盖的日期闭区间。无法判断时返回 None。"""
    title = title or ""
    pub = None
    if published:
        try:
            pub = date.fromisoformat(published[:10])
        except ValueError:
            pub = None
    year_m = re.search(r"(20\d{2})\s*年", title)
    year = int(year_m.group(1)) if year_m else (pub.year if pub else None)
    if year is None:
        return None

    range_m = re.search(
        r"(\d{1,2}|十二|十一|十|九|八|七|六|五|四|三|二|一)\s*月\s*(\d{1,2})\s*日"
        r"\s*[-~～到至]\s*(?:(\d{1,2}|十二|十一|十|九|八|七|六|五|四|三|二|一)\s*月\s*)?(\d{1,2})\s*日",
        title,
    )
    if range_m:
        m1 = _month_num(range_m.group(1))
        m2 = _month_num(range_m.group(3)) if range_m.group(3) else m1
        if m1 and m2:
            y2 = year + (1 if m2 < m1 else 0)
            try:
                return date(year, m1, int(range_m.group(2))), date(y2, m2, int(range_m.group(4)))
            except ValueError:
                return None

    month_m = re.search(r"(\d{1,2}|十二|十一|十|九|八|七|六|五|四|三|二|一)\s*月", title)
    month = _month_num(month_m.group(1)) if month_m else None
    if month is None:
        if "春节" in title:
            month = 2
        elif "五一" in title:
            month = 5
        elif "六一" in title:
            month = 6
    if month is None:
        return None
    # 「1月活动」发在上年 12 月
    if year_m is None and pub and pub.month == 12 and month <= 2:
        year = pub.year + 1
    try:
        start = date(year, month, 1)
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        return start, end
    except ValueError:
        return None


def select_current_month(items, today=None):
    """挑出覆盖今天所在月份的日程，新发布的在前。"""
    today = today or now_cn().date()
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = date(today.year, 12, 31)
    else:
        month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)
    picked = []
    for it in items:
        span = article_span(it.get("title", ""), it.get("date", ""))
        if not span:
            continue
        start, end = span
        if start <= month_end and end >= month_start:
            picked.append(it)
    picked.sort(key=lambda it: it.get("date") or "", reverse=True)
    return picked


def _series_in(text):
    best, best_at = None, 10 ** 9
    for key, booking in SERIES_RULES:
        at = text.find(key)
        if at >= 0 and at < best_at:
            best, best_at = (key, booking), at
    return best


def _classify_booking(text):
    t = re.sub(r"\s+", "", text or "")
    # 海报写明「不用预约」时以海报为准，不要被地点猜测盖掉
    if any(k in t for k in ("无需再次预约", "无需预约", "不用预约", "直接前往活动地点")):
        return "walkin"
    online = any(k in t for k in ("线上预约", "网上预约", "每周一中午12", "周一上午12", "周一中午12"))
    onsite = any(k in t for k in ("取票机", "上午9点", "上午9：00", "上午9:00"))
    if "现场预约" in t and "网上预约" in t:
        return "both"
    if online and onsite:
        return "both"
    if onsite:
        return "onsite_9am"
    if online:
        return "monday_noon"
    return None


def _age_text(text):
    m = re.search(r"年龄[^：:\n]{0,8}[：:]\s*([0-9岁及以上以下到至~\-～]{2,16})", text or "")
    if m:
        return m.group(1).strip("。，, ")
    m = AGE_RE.search(text or "")
    return m.group(0).replace(" ", "") if m else ""


def age_fits(age_text, child_age):
    if not age_text:
        return True
    m = re.search(r"(\d+)\s*[-~～到至]\s*(\d+)\s*岁", age_text)
    if m:
        return int(m.group(1)) <= child_age <= int(m.group(2))
    m = re.search(r"(\d+)\s*岁\s*(及以上|以上)", age_text)
    if m:
        return child_age >= int(m.group(1))
    m = re.search(r"(\d+)\s*岁\s*(及以下|以下)", age_text)
    if m:
        return child_age <= int(m.group(1))
    return True


def _norm_time(m):
    sh, sm, eh, em = (int(m.group(i)) for i in range(1, 5))
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return ""
    if eh * 60 + em <= sh * 60 + sm:
        return ""
    return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"


def _duration_min(span):
    try:
        a, b = span.split("-")
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
    except ValueError:
        return 0
    return (bh * 60 + bm) - (ah * 60 + am)


def resolve_booking(ctx_booking, place, time_span, series):
    """海报上的预约方式优先。只有海报没写时，才按地点猜。"""
    dur = _duration_min(time_span)
    place = place or ""
    if ctx_booking == "walkin":
        return "walkin"
    if ctx_booking in ("onsite_9am", "both", "monday_noon"):
        return ctx_booking
    if series and "炫酷" in series:
        return "onsite_9am"
    if series and "活字" in series:
        return "both"
    if "门口" in place and 0 < dur <= 25:
        return "walkin"
    if ctx_booking == "none":
        return "walkin"
    if dur >= 30:
        return "monday_noon"
    return "walkin"


def _poster_title(blob):
    """海报左上角的栏目名。跳过说明句。"""
    for line in blob.splitlines():
        line = re.sub(r"^[\"“”\s]+|[\"“”\s]+$", "", line.strip())
        if not line or len(line) > 12:
            continue
        if line.startswith("活动") or any(k in line for k in ("预约", "参与", "年龄", "对象")):
            continue
        if any(p in line for p in ("。", "，", "？", "！", "：", ":")):
            continue
        return line
    return ""


def _is_blurb(title):
    if len(title) > 12 or title.endswith(("！", "!", "。", "吧")):
        return True
    return False


def _ocr_image(path):
    """调色板 PNG 直接识别会丢字，先转 RGB 并放大。结果按图片内容缓存。"""
    from PIL import Image
    import numpy as np

    raw = Path(path).read_bytes()
    key = hashlib.md5(raw).hexdigest()
    cache = CACHE / "ocr" / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    im = Image.open(path).convert("RGB")
    if im.width < 1600:
        im = im.resize((im.width * 2, im.height * 2), Image.Resampling.LANCZOS)
    if _ocr_image.engine is None:
        print("[hbstm] 加载文字识别（第一次会下载模型）", flush=True)
        from rapidocr_onnxruntime import RapidOCR
        _ocr_image.engine = RapidOCR()
    result, _ = _ocr_image.engine(np.array(im))
    items = []
    for box, text, score in result or []:
        text = (text or "").strip()
        if not text or score < 0.55:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "text": text,
            "x": sum(xs) / 4,
            "y": sum(ys) / 4,
            "h": max(ys) - min(ys),
        })
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


_ocr_image.engine = None


def _download(url, dest):
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    r = fetchers.get(url, timeout=30, headers={"Referer": "https://mp.weixin.qq.com/"})
    if not r:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _is_table(items):
    blob = "".join(i["text"] for i in items)
    return "日期" in blob and "时间" in blob and DATE_RE.search(blob)


def _apply_poster(items, ctx):
    blob = "\n".join(i["text"] for i in items)
    hit = _series_in(blob)
    title = hit[0] if hit else _poster_title(blob)
    if title and title != ctx.get("series"):
        ctx["age"] = ""
        ctx["series"] = title
    if hit:
        ctx["booking"] = hit[1]
    booking = _classify_booking(blob)
    if booking:
        ctx["booking"] = booking
    age = _age_text(blob)
    if age:
        ctx["age"] = age
    ctx["poster_note"] = blob[:300]
    return ctx


def _clean_title(text):
    text = DATE_RE.sub("", text)
    text = TIME_RE.sub("", text)
    text = AGE_RE.sub("", text)
    text = PLACE_RE.sub("", text)
    text = re.sub(r"[\s|｜:：,，。·]+", "", text)
    if text in HEADER_WORDS or len(text) < 2:
        return ""
    return text[:40]


def parse_table(items, ctx, year, child_age, article_url):
    dates, times, others = [], [], []
    for it in items:
        if it["text"] in HEADER_WORDS:
            continue
        if DATE_RE.search(it["text"]) and not TIME_RE.search(it["text"]):
            dates.append(it)
            continue
        found_time = False
        for m in TIME_RE.finditer(it["text"]):
            norm = _norm_time(m)
            if norm:
                times.append({**it, "time": norm})
                found_time = True
        if not found_time:
            others.append(it)
    if not dates or not times:
        return []

    heights = sorted(i["h"] for i in items if i["h"] > 0) or [40]
    row_h = heights[len(heights) // 2]
    # 说明文字经常换行，跟时刻的垂直距离大约两行以内
    attach_px = max(row_h * 2.4, 36)

    for t in times:
        nearest = min(dates, key=lambda d: abs(d["y"] - t["y"]))
        dm = DATE_RE.search(nearest["text"])
        t["month"], t["day"] = int(dm.group(1)), int(dm.group(2))
        t["extras"] = []
    for ot in others:
        nearest = min(times, key=lambda t: abs(t["y"] - ot["y"]))
        if abs(nearest["y"] - ot["y"]) <= attach_px:
            nearest["extras"].append(ot)

    events = []
    for t in times:
        try:
            when = date(year, t["month"], t["day"])
        except ValueError:
            continue
        extras = sorted(t["extras"], key=lambda i: (i["y"], i["x"]))
        bits = []
        own = _clean_title(t["text"])
        if own:
            bits.append((t["x"], own))
        age, place = "", ""
        for ex in extras:
            raw = ex["text"].strip()
            if raw in HEADER_WORDS:
                continue
            am = AGE_RE.search(raw)
            if am:
                age = am.group(0).replace(" ", "")
            pm = PLACE_RE.search(raw)
            if pm and not place:
                place = pm.group(1)
            title = _clean_title(raw)
            if title:
                bits.append((ex["x"], title))
        if not age:
            age = ctx.get("age") or ""
        bits.sort()
        # 同一行里最靠左的短词是「活动名称」，右边长句是介绍
        shorts = [b for _, b in bits if len(b) <= 14]
        title = shorts[0] if shorts else (bits[0][1] if bits else "")
        if not title:
            continue
        booking = resolve_booking(ctx.get("booking"), place, t["time"], ctx.get("series", ""))
        if booking == "none":
            booking = "walkin"
        event = {
            "title": title,
            "series": ctx.get("series") or "",
            "date": when.isoformat(),
            "time": t["time"],
            "place": place,
            "age": age,
            "suitable": age_fits(age, child_age),
            "booking": booking,
            "url": article_url,
        }
        event["id"] = hashlib.md5(
            "|".join([event["date"], event["time"], event["title"], event["place"]]).encode()
        ).hexdigest()[:12]
        events.append(event)
    # 同一活动常排两场，名称会被拆开，或一半写成介绍句
    by_day = {}
    for e in events:
        by_day.setdefault((e["date"], e.get("series") or "", e.get("age") or "", e.get("place") or ""), []).append(e)
    for group in by_day.values():
        names = [e for e in group if e["title"] and not _is_blurb(e["title"])]
        blurbs = [e for e in group if _is_blurb(e["title"])]
        if len(names) == 1 and blurbs:
            for e in blurbs:
                e["title"] = names[0]["title"]
    # 补过标题后重算 id
    for e in events:
        e["id"] = hashlib.md5(
            "|".join([e["date"], e["time"], e["title"], e["place"]]).encode()
        ).hexdigest()[:12]
    return events


def parse_article(article, year, child_age):
    """按文档顺序走正文：海报更新栏目，紧随的表格产出场次。"""
    ctx = {"series": "", "booking": None, "age": ""}
    events = []
    buf = []
    paths = article.get("image_paths") or []
    # 重新解析 HTML 才能知道文字和图片的先后。调用方把 html 放在 article["html_path"]
    html_path = article.get("html_path")
    if not html_path or not Path(html_path).exists():
        return events
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(Path(html_path).read_bytes(), "html.parser")
    body = soup.select_one("#js_content")
    if not body:
        return events

    img_i = 0

    def flush_text():
        nonlocal buf, ctx
        chunk = "".join(buf)
        buf = []
        hit = _series_in(chunk)
        if hit and hit[0] != ctx.get("series"):
            ctx["age"] = ""
            ctx["series"], ctx["booking"] = hit
        booking = _classify_booking(chunk)
        if booking:
            ctx["booking"] = booking
        age = _age_text(chunk)
        if age:
            ctx["age"] = age

    for el in body.descendants:
        if getattr(el, "name", None) == "img":
            flush_text()
            if img_i >= len(paths) or not paths[img_i]:
                img_i += 1
                continue
            path = paths[img_i]
            img_i += 1
            print(f"[hbstm] 识别 {img_i}/{len(paths)}", flush=True)
            try:
                items = _ocr_image(path)
            except Exception as e:
                print(f"[hbstm] 识别失败 {path}: {e}")
                continue
            if not items:
                continue
            if _is_table(items):
                found = parse_table(items, ctx, year, child_age, article["url"])
                print(f"[hbstm] 表格 +{len(found)} 场 栏目={ctx.get('series') or '未分栏目'} "
                      f"预约={ctx.get('booking') or '自动'}")
                events.extend(found)
            else:
                before = (ctx.get("series"), ctx.get("booking"))
                _apply_poster(items, ctx)
                if (ctx.get("series"), ctx.get("booking")) != before:
                    print(f"[hbstm] 海报 栏目={ctx.get('series') or '—'} 预约={ctx.get('booking') or '—'}")
        elif el.name is None:
            s = str(el).strip()
            if s:
                buf.append(s + "\n")
    flush_text()

    # 只留目标年份里的场次，并去掉完全相同的识别重复
    kept, seen = [], set()
    for e in events:
        if not e["date"].startswith(f"{year:04d}-"):
            continue
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        kept.append(e)
    kept.sort(key=lambda e: (e["date"], e["time"], e["title"]))
    return kept


def _index_url():
    cfg = CONFIG.get("hbstm_schedule") or {}
    return cfg.get("index_url") or "https://www.hbstm.org.cn/webart/WebArtIdx/117/"


def build_schedule(today=None, picked=None):
    """抓列表、挑当月、识别课表。返回可写入 state 的字典（不含 reminded）。"""
    today = today or now_cn().date()
    child_age = int(CONFIG["child"]["age"])
    if picked is None:
        index_url = _index_url()
        print(f"[hbstm] 抓取活动日程 {index_url}")
        listing = fetchers.fetch_hbstm_schedule_index(index_url)
        print(f"[hbstm] 列表 {len(listing)} 条")
        picked = select_current_month(listing, today)
    if not picked:
        print(f"[hbstm] {today.year}年{today.month}月还没有活动日程")
        return {
            "month": f"{today.year:04d}-{today.month:02d}",
            "updated_at": now_cn().isoformat(timespec="seconds"),
            "articles": [],
            "events": [],
        }
    for it in picked:
        print(f"[hbstm] 当月日程：{it['date']} {it['title']}")

    events = []
    articles_meta = []
    for it in picked:
        print(f"[hbstm] 打开 {it['url']}")
        r = fetchers.get(it["url"])
        html_path = CACHE / "articles" / f"{hashlib.md5(it['url'].encode()).hexdigest()}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = r.content if r and b"js_content" in r.content else b""
        if fresh:
            html_path.write_bytes(fresh)
        elif html_path.exists() and b"js_content" in html_path.read_bytes():
            print("[hbstm] 这次没打开正文，沿用上次缓存")
        else:
            print("[hbstm] 正文被拦截，跳过")
            continue
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_path.read_bytes(), "html.parser")
        title_el = soup.select_one("#activity-name") or soup.find("title")
        body = soup.select_one("#js_content")
        # 跟 parse_article 里的 img 遍历保持同一顺序，没有地址的位置留空
        images = []
        if body:
            for img in body.select("img"):
                src = (img.get("data-src") or img.get("src") or "").strip()
                images.append(src.split("#")[0] if src.startswith("http") else "")
        title = (title_el.get_text(strip=True) if title_el else "") or it["title"]
        paths = []
        img_dir = CACHE / "imgs" / hashlib.md5(it["url"].encode()).hexdigest()
        for i, src in enumerate(images):
            if not src:
                paths.append("")
                continue
            ext = "png" if ".png" in src.split("?")[0] else "jpg"
            dest = img_dir / f"{i:02d}.{ext}"
            print(f"[hbstm] 图片 {i + 1}/{len(images)}", flush=True)
            saved = _download(src, dest)
            paths.append(str(saved) if saved else "")
        span = article_span(it["title"], it["date"])
        year = span[0].year if span else today.year
        parsed = parse_article(
            {"url": it["url"], "html_path": str(html_path), "image_paths": paths},
            year, child_age,
        )
        # 只保留与本月有交集的日期（一篇文章有时跨月）
        month_prefix = f"{today.year:04d}-{today.month:02d}"
        parsed = [e for e in parsed if e["date"].startswith(month_prefix)]
        print(f"[hbstm] {title} 识别出 {len(parsed)} 场")
        events.extend(parsed)
        articles_meta.append({
            "title": title or it["title"],
            "url": it["url"],
            "date": it.get("date") or "",
        })

    # 跨文章去重
    uniq = {}
    for e in events:
        uniq[e["id"]] = e
    events = sorted(uniq.values(), key=lambda e: (e["date"], e["time"], e["title"]))
    return {
        "month": f"{today.year:04d}-{today.month:02d}",
        "updated_at": now_cn().isoformat(timespec="seconds"),
        "articles": articles_meta,
        "events": events,
    }


def refresh(today=None, force=False):
    """重新识别并落盘。文章链接没变时沿用缓存，已提醒过的场次 id 保留。"""
    today = today or now_cn().date()
    old = load_state()
    month = f"{today.year:04d}-{today.month:02d}"
    print(f"[hbstm] 抓取活动日程 {_index_url()}")
    listing = fetchers.fetch_hbstm_schedule_index(_index_url())
    print(f"[hbstm] 列表 {len(listing)} 条")
    picked = select_current_month(listing, today)
    urls = [it["url"] for it in picked]
    old_urls = [a.get("url") for a in old.get("articles") or []]
    if (not force and old.get("events") and old.get("month") == month and old_urls == urls):
        print("[hbstm] 当月文章没变，沿用已识别的课表")
        return old, False
    fresh = build_schedule(today, picked=picked)
    fresh["reminded"] = {
        k: v for k, v in (old.get("reminded") or {}).items()
        if k.split(":")[0] in {e["id"] for e in fresh["events"]}
    }
    save_state(fresh)
    first = not old.get("events")
    changed = urls != old_urls or first
    return fresh, changed


BOOKING_LABEL = {
    "monday_noon": "线上预约 · 当周周一 12:00 开抢",
    "both": "线上（当周周一 12:00）或当天现场取票",
    "onsite_9am": "当天 9:00 现场取票机",
    "walkin": "不用预约，到点在展厅看",
    "none": "不用预约",
}


def _fmt_dates(dates):
    days = []
    for d in dates:
        dt = date.fromisoformat(d)
        days.append(f"{dt.month}/{dt.day}")
    if len(days) <= 8:
        return "、".join(days)
    return "、".join(days[:8]) + f" 等{len(days)}天"


def group_events(events):
    groups = {}
    for e in events:
        key = (e.get("series") or "", e["title"], e.get("time") or "",
               e.get("place") or "", e.get("age") or "", e.get("booking") or "",
               bool(e.get("suitable", True)))
        groups.setdefault(key, []).append(e["date"])
    out = []
    for key, dates in groups.items():
        series, title, time_span, place, age, booking, suitable = key
        out.append({
            "series": series, "title": title, "time": time_span, "place": place,
            "age": age, "booking": booking, "suitable": suitable,
            "dates": sorted(set(dates)),
        })
    out.sort(key=lambda g: (g["dates"][0], g["time"], g["title"]))
    return out


def _bullet(g):
    head = g["title"]
    if g["series"] and g["series"] not in g["title"]:
        head = f"{g['series']} · {g['title']}"
    extra = " ".join(p for p in (g["time"], g["place"]) if p)
    age = f"（{g['age']}）" if g["age"] else ""
    return f"- {head}　{_fmt_dates(g['dates'])} {extra}{age}".rstrip()


def digest_markdown(state, today=None):
    today = today or now_cn().date()
    child = CONFIG["child"]["age"]
    articles = state.get("articles") or []
    events = state.get("events") or []
    month = state.get("month") or f"{today.year:04d}-{today.month:02d}"
    y, m = int(month[:4]), int(month[5:7])
    lines = []
    if articles:
        a = articles[0]
        lines.append(f"**[{a['title']}]({a['url']})**")
        if len(articles) > 1:
            for extra in articles[1:]:
                lines.append(f"另有 [{extra['title']}]({extra['url']})")
    else:
        lines.append(f"{y}年{m}月的活动日程还没挂出来。")
        return "\n".join(lines)

    upcoming = [e for e in events if e["date"] >= today.isoformat()]
    past_n = len(events) - len(upcoming)
    suitable = [e for e in upcoming if e.get("suitable", True)]
    skipped = [e for e in upcoming if not e.get("suitable", True)]

    lines.append(f"孩子 {child} 岁。下面是还没过期、年龄对得上的场次。")
    if not suitable:
        lines.append("这个月还没识别到适龄场次。")
    order = ["monday_noon", "both", "onsite_9am", "walkin"]
    for booking in order:
        chunk = [e for e in suitable if e.get("booking") == booking]
        if not chunk:
            continue
        lines.append("")
        lines.append(f"**{BOOKING_LABEL.get(booking, booking)}**")
        if booking in ("monday_noon", "both"):
            lines.append("入口：公众号「湖北省科学技术馆」→ 票务预约 → 课程预约。约满即止。")
        elif booking == "onsite_9am":
            lines.append("南门入口和科普报告厅对面的预约机，上午 9:00 开始，每场约 200 张，约满即止。带身份证。")
        bullets = [_bullet(g) for g in group_events(chunk)]
        cap = 25
        lines.extend(bullets[:cap])
        if len(bullets) > cap:
            lines.append(f"- …还有 {len(bullets) - cap} 组，打开原文看全表")
    if skipped:
        lines.append("")
        lines.append(f"年龄对不上的还有 {len(group_events(skipped))} 组（含 8 岁以上的活字讲坛等），这次不提醒预约。")
    if past_n:
        lines.append(f"已结束的场次 {past_n} 场，不再列出。")
    lines.append("")
    lines.append("周一 11:50 提醒抢当周线上课程，活动当天 8:30 提醒现场取票。")
    return "\n".join(lines)


def digest_card(state, today=None):
    today = today or now_cn().date()
    month = state.get("month") or today.strftime("%Y-%m")
    y, m = int(month[:4]), int(month[5:7])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"🦕 省科技馆 {y}年{m}月活动"},
        },
        "elements": [{"tag": "markdown", "content": digest_markdown(state, today)}],
    }


def _parse_hhmm(value, default):
    raw = value or default
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


def due_events(state, today=None):
    """返回 (kind, event) 。kind 为 online 或 onsite。只在提醒窗口内。"""
    now = today or now_cn()
    if isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    cfg = CONFIG.get("hbstm_schedule") or {}
    window = int(cfg.get("remind_window_minutes") or 90)
    online_at = _parse_hhmm(cfg.get("online_remind"), "11:50")
    onsite_at = _parse_hhmm(cfg.get("onsite_remind"), "08:30")
    reminded = state.get("reminded") or {}
    out = []
    for e in state.get("events") or []:
        if not e.get("suitable", True):
            continue
        try:
            ev_day = date.fromisoformat(e["date"])
        except ValueError:
            continue
        booking = e.get("booking")
        if booking in ("monday_noon", "both"):
            monday = ev_day - timedelta(days=ev_day.weekday())
            start = datetime.combine(monday, online_at, TZ)
            if start <= now < start + timedelta(minutes=window):
                key = f"{e['id']}:online"
                if key not in reminded:
                    out.append(("online", e))
        if booking in ("onsite_9am", "both"):
            start = datetime.combine(ev_day, onsite_at, TZ)
            if start <= now < start + timedelta(minutes=window):
                key = f"{e['id']}:onsite"
                if key not in reminded:
                    out.append(("onsite", e))
    return out


def remind_markdown(pairs):
    online = [e for k, e in pairs if k == "online"]
    onsite = [e for k, e in pairs if k == "onsite"]
    lines = []
    if online:
        lines.append("**今天 12:00 开抢当周课程**（先到先得）")
        lines.append("公众号「湖北省科学技术馆」→ 票务预约 → 课程预约")
        lines.append("一个订单最多 3 个孩子，提前几分钟进小程序等着。")
        lines.append("")
        for g in group_events(online):
            lines.append(_bullet(g))
    if onsite:
        if lines:
            lines.append("")
        lines.append("**今天 9:00 现场取票**")
        lines.append("南门入口、科普报告厅对面预约机。带身份证，每场约 200 张。")
        lines.append("")
        for g in group_events(onsite):
            lines.append(_bullet(g))
    return "\n".join(lines)


def remind_card(pairs):
    kinds = {k for k, _ in pairs}
    if kinds == {"onsite"}:
        title = "⏰ 省科技馆 · 今天 9:00 现场取票"
    elif kinds == {"online"}:
        title = "⏰ 省科技馆 · 今天 12:00 开抢"
    else:
        title = "⏰ 省科技馆 · 该预约了"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "markdown", "content": remind_markdown(pairs)}],
    }


def mark_reminded(state, pairs):
    reminded = dict(state.get("reminded") or {})
    stamp = now_cn().isoformat(timespec="seconds")
    for kind, e in pairs:
        reminded[f"{e['id']}:{kind}"] = stamp
    state["reminded"] = reminded
    save_state(state)


def run(do_refresh=False, do_remind=False, dry_run=False, force=False, push=None):
    """抓取和/或提醒。push(card) 由 pipeline 注入；dry_run 只打印。"""
    state = load_state()
    send_digest = False
    if do_refresh or (do_remind and not state.get("events")):
        state, send_digest = refresh(force=force)
    if do_refresh:
        card = digest_card(state)
        if dry_run or push is None:
            print("\n===== 当月活动 =====\n")
            print(card["elements"][0]["content"])
            print("\n===== 结束 =====\n")
        elif send_digest:
            push(card)
        else:
            print("[hbstm] 日程没有换文章，不重复推送月报")

    if not do_remind:
        return state

    # 月报刚刷新时 state 已是最新；只提醒时读盘
    if not do_refresh:
        state = load_state()
    pairs = due_events(state)
    if not pairs:
        if not state.get("events"):
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"template": "orange", "title": {"tag": "plain_text", "content": "⏰ 抢课闹钟 · 12:00 开抢"}},
                "elements": [{"tag": "markdown", "content": (
                    "**湖北省科技馆本周课程 12:00 开放预约**（先到先得）\n\n"
                    "📱 入口：公众号「湖北省科学技术馆」→ 票务预约 → 课程预约\n"
                    "这次没能读到当月课表，先按固定时间提醒。\n"
                )}],
            }
            print("[hbstm] 没有课表，改推固定提醒")
            if dry_run or push is None:
                print(card["elements"][0]["content"])
            else:
                push(card)
            return state
        print("[hbstm] 这个时间点没有要提醒的预约")
        return state
    card = remind_card(pairs)
    if dry_run or push is None:
        print("\n===== 预约提醒 =====\n")
        print(card["elements"][0]["content"])
        print("\n===== 结束 =====\n")
        return state
    push(card)
    mark_reminded(state, pairs)
    print(f"[hbstm] 已提醒 {len(pairs)} 场")
    return state
