import os, re
from datetime import datetime, timedelta
from dateutil import tz, parser as dateparser
from slack_sdk import WebClient

JST = tz.gettz("Asia/Tokyo")
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

SRC = [s.strip() for s in os.environ["SRC_CHANNELS"].split(",")]
DEST = os.environ["DEST_CHANNEL"]

# 環境可変の運用パラメータ
POST_WINDOW_DAYS = int(os.environ.get("POST_WINDOW_DAYS", 14))
CLOSE_REACTIONS = [s.strip() for s in os.environ.get("CLOSE_REACTIONS","no_entry,x,white_check_mark").split(',')]
CLOSE_KEYWORDS = [s.strip().lower() for s in os.environ.get("CLOSE_KEYWORDS", "締切,〆切,クローズ,closed,close").split(',')]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

now = datetime.now(JST)
RANGE_FROM = now
RANGE_TO = now + timedelta(days=POST_WINDOW_DAYS)

# 抽出用のざっくり正規表現
EVENT_RE = re.compile(r"^■\s*イベント名\s*\n(.+)$", re.MULTILINE)
DATE_LINE_RE = re.compile(r"^■\s*日時\s*\n(.+)$", re.MULTILINE)
PLACE_RE = re.compile(r"^■\s*場所\s*\n(.+)$", re.MULTILINE)

# 日付部分だけ抜く（例: 2026.02.01, 2026/02/01, 2026-02-01）
DATE_ONLY_RE = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")

def load_category_map():
    m = {}
    def add(env_key, label):
        ids = os.environ.get(env_key, "")
        for cid in [x.strip() for x in ids.split(",") if x.strip()]:
            m[cid] = label
    add("FREE_CHANNELS", "無料ライブ")
    add("PAID_CHANNELS", "チャージありライブ")
    add("OTHER_CHANNELS", "その他")
    return m

CATEGORY_BY_CHANNEL = load_category_map()
CATEGORY_ORDER = ["無料ライブ", "チャージありライブ", "その他"]

def parse_fields(text):
    # イベント名
    m = EVENT_RE.search(text)
    title = m.group(1).strip() if m else None

    # 場所
    mp = PLACE_RE.search(text)
    place = mp.group(1).strip() if mp else None

    # 日時（行全体 → 日付だけ抜く → 23:59を補う）
    md_line = DATE_LINE_RE.search(text)
    when = None
    if md_line:
        line = md_line.group(1).strip()
        md = DATE_ONLY_RE.search(line)
        if md:
            y, mo, d = md.group(1), md.group(2), md.group(3)
            dt_str = f"{y}-{int(mo):02d}-{int(d):02d} 23:59"
            try:
                when = dateparser.parse(dt_str).replace(tzinfo=JST)
            except Exception:
                when = None

    return title, when, place


 

def is_closed(parent_ts, channel):
    # 親リアクション
    rx = client.reactions_get(channel=channel, timestamp=parent_ts)
    reactions = []
    if "message" in rx and rx["message"].get("reactions"):
        reactions = rx["message"]["reactions"]
    for r in reactions:
        if r.get("name") in CLOSE_REACTIONS:
            return True
    # スレッド返信のキーワード
    replies = client.conversations_replies(channel=channel, ts=parent_ts, limit=200)
    for msg in replies.get("messages", [])[1:]:
        txt = (msg.get("text") or "").lower()
        if any(k in txt for k in CLOSE_KEYWORDS):
            return True
    return False

def fetch_messages(ch):
    messages = []
    res = client.conversations_history(channel=ch, limit=200)
    messages.extend(res.get("messages", []))
    while res.get("has_more"):
        res = client.conversations_history(channel=ch, cursor=res["response_metadata"]["next_cursor"], limit=200)
        messages.extend(res.get("messages", []))
    return messages

def collect_events():
    events = []
    for ch in SRC:
        msgs = fetch_messages(ch)
        for m in msgs:
            if m.get("subtype"):
                continue # bot_message等を除外
            text = m.get("text","")
            title, when, place = parse_fields(text)
            if not (title and when and place):
                continue
            if when < now:
                continue
            if is_closed(m["ts"], ch):
                continue
            # 親パーマリンク
            perma = client.chat_getPermalink(channel=ch, message_ts=m["ts"]).get("permalink")
            # チャンネル名
            info = client.conversations_info(channel=ch)
            category = CATEGORY_BY_CHANNEL.get(ch, "その他")  # 未設定ならその他扱い

            events.append({
                "ts": m["ts"],
                "channel": ch,
                "category": category,
                "title": title,
                "when": when,
                "place": place,
                "permalink": perma
            })

    # 日時昇順
    events.sort(key=lambda e: e["when"])
    return events

def format_blocks(events):
    header = "↓↓現在募集中のイベント！！↓↓"

    if not events:
        return [{"type":"section","text":{"type":"mrkdwn","text": header + "\n掲載可能なイベントはありませんでした。"}}]

    # category -> list にまとめる
    grouped = {k: [] for k in CATEGORY_ORDER}
    for e in events:
        grouped.setdefault(e["category"], []).append(e)

    blocks = [{"type":"section","text":{"type":"mrkdwn","text": header}}]

    for cat in CATEGORY_ORDER:
        lst = grouped.get(cat, [])
        if not lst:
            text = f"*** {cat} ***\n（現在募集はありません）"
            blocks.append({"type":"section","text":{"type":"mrkdwn","text": text}})
            continue

        lines = []
        for i, e in enumerate(lst, 1):
            title_link = f"<{e['permalink']}|{e['title']}>"
            lines.append(f"{i}. {e['when'].strftime('%m/%d(%a)')}ー{title_link}（{e['place']}）")

        text = f"*** {cat} ***\n" + "\n".join(lines)
        blocks.append({"type":"section","text":{"type":"mrkdwn","text": text}})

    blocks.append({
        "type":"context",
        "elements":[{"type":"mrkdwn","text":"※ スレッド『締切』返信 or 指定リアクション付き／過去日時は掲載していません"}]
    })
    return blocks


def run():
    events = collect_events()
    blocks = format_blocks(events)
    if DRY_RUN:
        # 確認用、投稿せず内容をログに
        print(blocks)
        return
    client.chat_postMessage(channel=DEST, text="週次イベントまとめ", blocks=blocks)

if __name__ == "__main__":
    run()

