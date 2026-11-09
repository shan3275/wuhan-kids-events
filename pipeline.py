#!/usr/bin/env python3
"""光谷遛娃活动情报 — 主流程。

每周日/周三 19:30（北京时间）由 GitHub Actions 定时运行：
  1. 抓取候选（官网直抓 + Bing RSS 关键词轮询）
  2. 按 URL 去重（state/seen.json 持久化）
  3. 抓文章正文，交给 Claude 提取结构化活动信息并按规则筛选
  4. 生成飞书卡片推送到群机器人
  5. 已处理的 URL 写回 seen.json（随仓库提交，跨运行去重）

本地运行：
  python pipeline.py --dry-run        # 抓取+提取，但不推送，打印卡片
  python pipeline.py --init           # 首跑：把当前所有内容标记为已见，不推送
  python pipeline.py --hbstm          # 省科技馆当月活动日程（识别长图并推送月报）
  python pipeline.py --remind         # 到点提醒预约（周一 12:00 / 当天 9:00）
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import fetchers

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "state" / "seen.json"

# Claude 提取的 JSON Schema：一次请求处理本批全部文章
EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "venue": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "location": {"type": "string"},
                    "age_range": {"type": "string"},
                    "fee": {"type": "string"},
                    "registration": {"type": "string"},
                    "suitable": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "date", "location", "suitable", "url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", {})
    return {}


def save_state(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"seen": seen}, ensure_ascii=False, indent=1), encoding="utf-8")


def collect_candidates(seen):
    """汇总所有抓取通道的候选：按 URL 去重、过滤已见和明显无关标题。"""
    candidates, seen_urls = [], set()
    exclude = CONFIG["exclude_title_keywords"]

    for src in CONFIG["sources"]["direct"]:
        items = []
        if src["venue"] == "光谷书房":
            items = fetchers.fetch_library_chutian(src["url"])
        elif src["venue"] == "湖北省科技馆":
            items = fetchers.fetch_hbstm_homepage(src["url"])
        for it in items:
            it["venue_hint"] = src["venue"]
            candidates.append(it)

    for group in CONFIG["sources"]["searches"]:
        for q in group["queries"]:
            for it in fetchers.bing_search(q):
                it["venue_hint"] = group["venue"]
                candidates.append(it)
            time.sleep(1)

    out = []
    for c in candidates:
        url = c.get("url", "")
        title = c.get("title", "")
        if not url or url in seen_urls:
            continue
        if any(k in title for k in exclude):
            continue
        if any(k in url for k in CONFIG.get("url_blacklist", [])):
            continue
        if hashlib.md5(url.encode()).hexdigest() in seen:
            continue
        seen_urls.add(url)
        out.append(c)
    limit = CONFIG["max_articles_per_run"]
    print(f"[collect] 共 {len(out)} 个新候选（去重后），最多处理 {limit} 个")
    return out[:limit]


def build_extraction_prompt(articles):
    """把本批文章拼成一次提取请求的用户输入。"""
    cfg = CONFIG
    today = datetime.now().strftime("%Y-%m-%d %A")
    lines = [
        f"今天是 {today}。孩子的年龄是 {cfg['child']['age']} 岁，家在武汉东湖高新区（{cfg['home']['label']}附近）。",
        "下面是各个渠道抓到的近期文章，请从中提取面向儿童/亲子的活动信息。",
        "",
    ]
    for i, a in enumerate(articles, 1):
        lines.append(f"### 文章 {i}")
        lines.append(f"标题：{a.get('title', '')}")
        lines.append(f"来源：{a.get('source', '')} / {a.get('venue_hint', '')}")
        lines.append(f"链接：{a['url']}")
        if a.get("date"):
            lines.append(f"发布日期：{a['date']}")
        lines.append(f"正文（截断）：\n{a.get('text', '')[:cfg['max_article_text_chars']]}")
        lines.append("")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是武汉光谷的遛娃活动情报助手。从给定文章中提取面向儿童或亲子的活动，"
    "并判断是否适合一个 7 岁（小学一二年级）的孩子。\n"
    "判断 suitable=true 的标准：\n"
    "- 年龄段匹配（6-9 岁可参加，写的是'儿童''亲子'但没写年龄也算）；\n"
    "- 地点在武汉东湖高新区/光谷一带，或距离不算远；\n"
    "- 活动类型是科普、阅读、手工、美育、演出、研学、自然观察等；\n"
    "- 免费或价格合理（百元内）。\n"
    "- 活动日期已经过去的（早于今天）一律 suitable=false。\n"
    "报名信息（报名开放时间、名额、报名方式）非常重要，必须尽量提取，"
    "尤其注意'几点开抢''限几人''先到先得'这类信息。\n"
    "一篇文章如果包含多个活动，拆成多条。与活动无关的文章（获奖新闻、公告等）不要输出。\n"
    "字段填'未知'表示文章里没有提到。date 尽量用 YYYY-MM-DD 格式；活动时间跨多天写起止。\n"
    "只输出 JSON，不要输出任何其他文字或代码围栏。"
)


def resolve_model():
    """模型名解析：显式配置优先，其次跟随会话环境（DeepSeek 等兼容端点）。"""
    return (os.environ.get("ACTIVITY_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
            or "claude-opus-5-5")


def extract_with_claude(articles):
    """一次请求提取全部文章的结构化活动信息并筛选。

    说明：结构化输出用 output_config.format（官方端点会强制执行 schema）；
    解析侧仍兼容 markdown 围栏等松散输出（网关/兼容端点不保证约束）。
    DeepSeek 等兼容端点不认识 output_config 时自动去掉重试。
    被拒绝时由 main 的 except 自动降级为标题列表，效果等价。
    """
    import anthropic

    system = SYSTEM_PROMPT
    model = resolve_model()
    client = anthropic.Anthropic()
    kwargs = dict(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": build_extraction_prompt(articles)}],
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": EVENT_SCHEMA},
        },
    )
    try:
        resp = client.messages.create(**kwargs)
    except (anthropic.BadRequestError, TypeError) as e:
        print(f"[claude] output_config 不被支持（{type(e).__name__}），去掉后重试")
        kwargs.pop("output_config")
        resp = client.messages.create(**kwargs)
    print(f"[claude] 模型 {model}")
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"模型拒绝处理: {getattr(resp.stop_details, 'category', '未知')}")
    texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
    if not texts:
        raise RuntimeError(f"模型未返回文本 (stop_reason={resp.stop_reason})")
    data = json.loads(strip_code_fences(texts[0]))
    usage = getattr(resp, "usage", None)
    if usage:
        print(f"[claude] 输入 {usage.input_tokens} tokens / 输出 {usage.output_tokens} tokens")
    events = normalize_events(data)
    if not events and usage and usage.output_tokens > 200:
        # 输出量不小却一条都没解析出来，多半是结构不认识，留个现场
        print(f"[claude] 警告：归一化后为空，原始返回前 300 字: {texts[0][:300]!r}")
    return events


# 字段别名：schema 不被强制的环境下，模型可能用不同的字段名/结构
FIELD_ALIASES = {
    "title": ["title", "name", "活动名称"],
    "url": ["url", "link", "链接", "活动链接"],
    "date": ["date", "activity_date", "activity_time", "published_at", "日期"],
    "time": ["time", "时间"],
    "location": ["location", "地点", "address"],
    "age_range": ["age_range", "target_audience", "年龄", "audience"],
    "fee": ["fee", "费用", "price"],
    "registration": ["registration", "报名", "registration_info"],
    "suitable": ["suitable", "suitable_for_7_year_old", "适合"],
    "reason": ["reason", "notes", "说明"],
    "venue": ["venue", "source", "场馆"],
}


def normalize_events(data):
    """把模型返回的各种形状/字段名归一化为标准事件结构。"""
    if isinstance(data, dict):
        raw = None
        for k in ("events", "activities"):
            if isinstance(data.get(k), list):
                raw = data[k]
                break
        if raw is None:
            raw = data.get("活动", [])
        if raw is None:
            # 单条活动直接作为对象返回的情况
            raw = [data] if any(a in data for a in FIELD_ALIASES["title"]) else []
    elif isinstance(data, list):
        raw = data
    else:
        raise RuntimeError(f"无法识别的返回结构: {type(data)}")

    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        norm = {}
        for canon, aliases in FIELD_ALIASES.items():
            for a in aliases:
                v = e.get(a)
                if v not in (None, "", []):
                    norm[canon] = v
                    break
        s = norm.get("suitable")
        if isinstance(s, str):
            norm["suitable"] = s.strip().lower() in ("true", "yes", "是", "适合", "1")
        # 模型单独输出"已过期/已结束"状态时，一律视为不适合
        status = str(e.get("status", ""))
        if "过期" in status or "结束" in status:
            norm["suitable"] = False
        out.append(norm)
    return out


def strip_code_fences(text):
    """容忍模型把 JSON 包在 ```json 围栏里（网关/低约束环境下可能发生）。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def fallback_extract(articles):
    """没有 API 凭证时的降级：只列标题，不做 AI 筛选。"""
    print("[claude] 未检测到 API 凭证，跳过 AI 提取（只列标题）")
    return [
        {
            "title": a.get("title") or a["url"],
            "venue": a.get("venue_hint", ""),
            "date": a.get("date", "未知"),
            "time": "未知",
            "location": "未知",
            "age_range": "未知",
            "fee": "未知",
            "registration": "未知",
            "suitable": False,
            "reason": "未启用 AI 筛选",
            "url": a["url"],
        }
        for a in articles
    ]


def _g(e, key, default="未知"):
    """防御式取值：schema 不被强制时模型可能漏字段。"""
    v = e.get(key)
    return str(v).strip() if v else default


def build_card(events, today):
    """生成飞书交互卡片（lark_md）。"""
    events = [e for e in events if isinstance(e, dict)]
    suitable = [e for e in events if e.get("suitable")]
    others = [e for e in events if not e.get("suitable")]

    lines = []
    if suitable:
        for e in suitable[:12]:
            title, url = _g(e, "title"), _g(e, "url", "")
            head = f"[{title}]({url})" if url else f"**{title}**"
            lines.append(f"**{head}**")
            lines.append(
                f"🕐 {_g(e, 'date')} {_g(e, 'time', '')} ｜ 📍 {_g(e, 'location')}\n"
                f"👧 年龄：{_g(e, 'age_range')} ｜ 💰 {_g(e, 'fee')}"
            )
            reg = _g(e, "registration")
            if reg != "未知":
                lines.append(f"✍️ 报名：{reg}")
            if _g(e, "reason", "") != "未知":
                lines.append(f"⭐ {_g(e, 'reason', '')}")
            lines.append("")
    else:
        lines.append("本周没有发现适合的新活动。")

    if others:
        lines.append("---")
        lines.append("**其他新动态**（暂不匹配，可点开看看）")
        for e in others[:8]:
            title, url = _g(e, "title"), _g(e, "url", "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")

    lines.append("---")
    lines.append(
        "🤖 每周三、周日 19:30 自动扫描 ｜ 公众号报名入口还需每周手动扫一眼："
        "光谷书房、湖北省科学技术馆、光谷图书馆、光谷美术馆、中国建筑科技馆"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"🦖 光谷遛娃情报 · {today}"},
        },
        "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
    }


def push_feishu(card):
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("[push] FEISHU_WEBHOOK 未设置，跳过推送")
        return
    url = webhook
    secret = os.environ.get("FEISHU_SECRET")
    if secret:
        # 飞书自定义机器人加签：HMAC-SHA256(key=timestamp+"\n"+secret, msg="")
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(string_to_sign.encode(), b"", hashlib.sha256).digest()
        sign = base64.b64encode(digest).decode()
        url = f"{webhook}{'&' if '?' in webhook else '?'}timestamp={ts}&sign={sign}"
    r = requests.post(url, json={"msg_type": "interactive", "card": card}, timeout=15)
    body = r.json()
    ok = body.get("code") == 0 or body.get("StatusCode") == 0
    print(f"[push] 飞书返回 {'成功' if ok else body}")
    if not ok:
        raise SystemExit(f"飞书推送失败: {body}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不推送，打印卡片内容")
    ap.add_argument("--init", action="store_true", help="首跑：标记全部为已见，不推送")
    ap.add_argument("--alarm", action="store_true", help="预约提醒（同 --remind）")
    ap.add_argument("--hbstm", action="store_true", help="抓取省科技馆当月活动日程并整理")
    ap.add_argument("--remind", action="store_true", help="到点提醒省科技馆预约")
    ap.add_argument("--force", action="store_true", help="当月文章没变也重新识别课表")
    args = ap.parse_args()

    if args.alarm:
        args.remind = True
    if args.hbstm or args.remind:
        import hbstm
        hbstm.run(
            do_refresh=args.hbstm,
            do_remind=args.remind,
            dry_run=args.dry_run,
            force=args.force,
            push=None if args.dry_run else push_feishu,
        )
        return

    seen = load_state()
    candidates = collect_candidates(seen)

    if args.init:
        for c in candidates:
            seen[hashlib.md5(c["url"].encode()).hexdigest()] = {
                "url": c["url"], "title": c.get("title", ""), "first_seen": time.time(),
            }
        save_state(seen)
        print(f"[init] 已标记 {len(candidates)} 条为已见，下次运行只看新内容")
        return

    if not candidates:
        print("[scan] 没有新内容，本次不推送")
        return

    articles = []
    for c in candidates:
        art = fetchers.fetch_article(c["url"], max_chars=CONFIG["max_article_text_chars"])
        art.update({"date": c.get("date", ""), "source": c.get("source", "搜索"),
                    "venue_hint": c.get("venue_hint", "")})
        art.setdefault("title", c.get("title", ""))
        articles.append(art)

    # 标记为已见：无论提取结果如何，本批 URL 都不再重复处理
    for c in candidates:
        seen[hashlib.md5(c["url"].encode()).hexdigest()] = {
            "url": c["url"], "title": c.get("title", ""), "first_seen": time.time(),
        }
    save_state(seen)

    try:
        events = extract_with_claude(articles)
    except Exception as e:  # 凭证缺失/网络/参数问题都降级，保证推送不中断
        print(f"[claude] 提取失败（{e}），降级为标题列表")
        events = fallback_extract(articles)

    today = datetime.now().strftime("%m月%d日")
    card = build_card(events, today)

    if args.dry_run:
        print("\n===== 飞书卡片预览 =====\n")
        print(card["elements"][0]["content"])
        print("\n===== 预览结束 =====\n")
        return
    push_feishu(card)


if __name__ == "__main__":
    main()
