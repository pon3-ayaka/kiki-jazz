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

DATE_TOKEN_RE = re.compile(
    r"""
    (?P<y>\d{4})\s*(?:[./\-年\s])\s*
    (?P<m>\d{1,2})\s*(?:[./\-月\s])\s*
    (?P<d>\d{1,2})\s*(?:[日]?) |
    (?P<m2>\d{1,2})\s*(?:[./\-月\s])\s*
    (?P<d2>\d{1,2})\s*(?:[日]?)
    """,
    re.VERBOSE
)

WEEKDAY_NOISE_RE = re.compile(r"[（(]?[月火水木金土日](?:曜|曜日)?[)）]?")

def parse_event_date(line: str, now_jst: datetime) -> datetime | None:
    s = line.strip()
    s = WEEKDAY_NOISE_RE.sub("", s)
    s = s.replace("　", " ")
    s = re.sub(r"\s+", " ", s)

    m = DATE_TOKEN_RE.search(s)
    if not m:
        return None

    # 時刻が書いてあれば拾う（例: 19:30）。無ければ 23:59
    tm = re.search(r"(\d{1,2}:\d{2})", s)
    hhmm = tm.group(1) if tm else "23:59"

    if m.group("y"):
        # 年あり
        y = int(m.group("y"))
        mo = int(m.group("m"))
        d = int(m.group("d"))
        dt_str = f"{y:04d}-{mo:02d}-{d:02d} {hhmm}"
        try:
            return dateparser.parse(dt_str).replace(tzinfo=JST)
        except Exception:
            return None
    else:
        # 年なし → まず今年で作り、過去なら来年にする（未来になる方）
        mo = int(m.group("m2"))
        d = int(m.group("d2"))

        for y in (now_jst.year, now_jst.year + 1):
            dt_str = f"{y:04d}-{mo:02d}-{d:02d} {hhmm}"
            try:
                cand = dateparser.parse(dt_str).replace(tzinfo=JST)
            except Exception:
                continue
            if cand >= now_jst:
                return cand

        # ここに来るのは基本レア（パース失敗が続いた等）
        return None


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
        when = parse_event_date(line, now)
        # if md:
        #     y, mo, d = md.group(1), md.group(2), md.group(3)
        #     dt_str = f"{y}-{int(mo):02d}-{int(d):02d} 23:59"
        #     try:
        #         when = dateparser.parse(dt_str).replace(tzinfo=JST)
        #     except Exception:
        #         when = None

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
    header = "📢✨*金曜配信！募集中イベント*✨📢\n気になるイベントがないかチェック👀\nイベント名リンクから募集スレッドに飛べるよ！"

    category_emoji = {
        "無料ライブ": "🆓",
        "チャージありライブ": "🎺",
        "その他": "🎈",
    }

    blocks = [{"type":"section","text":{"type":"mrkdwn","text": header}}]

    grouped = {k: [] for k in CATEGORY_ORDER}
    for e in events:
        grouped.setdefault(e["category"], []).append(e)

    for cat in CATEGORY_ORDER:
        lst = grouped.get(cat, [])
        if not lst:
            continue 

        lines = []
        for e in lst:
            title_link = f"<{e['permalink']}|{e['title']}>"
            lines.append(f"• {e['when'].strftime('%m/%d(%a)')}: {title_link}（{e['place']}）")

        emoji = category_emoji.get(cat, "📌")
        text = f"*{emoji} {cat}*\n" + "\n".join(lines)
        blocks.append({"type":"section","text":{"type":"mrkdwn","text": text}})

    return blocks



def run():
    events = collect_events()

    if not events:
        print("No open events found. Skip posting.")
        return

    blocks = format_blocks(events)

    if DRY_RUN:
        print(blocks)
        return

    client.chat_postMessage(channel=DEST, text="週次イベントまとめ", blocks=blocks)

if __name__ == "__main__":
    run()

