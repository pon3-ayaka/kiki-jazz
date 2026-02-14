import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import sys

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

if DRY_RUN:
    print("=== DRY RUN MODE ===")
    os.environ["SLACK_BOT_TOKEN"] = os.environ.get("SLACK_BOT_TOKEN_DEBUG")
    os.environ["SRC_CHANNELS"] = os.environ.get("SRC_CHANNELS_DEBUG")

SRC = [s.strip() for s in os.environ["SRC_CHANNELS"].split(",")]

TEMPLATE_TEXT = (
"""■ イベント名
（例）○○ライブ
■ 日時
（例）2026.4.30
（例）2026/3/2(月)-4/22(水)
（例）未定
■ 場所
（例）Blue Note Tokyo
■ 内容
イベントなど募集の際はこちらのテンプレートをコピーして、必要な情報を記載してください。
ここの欄には好きなこと、自由に書いてください！
"""
)
def env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def main() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")

    client = WebClient(token=token)

    print("[run] template preview (first 120 chars):", TEMPLATE_TEXT.replace("\n", "\\n")[:120])

    for channel in SRC:
        try:
            res = client.chat_postMessage(
                channel=channel,
                text=TEMPLATE_TEXT,
                unfurl_links=False,
                unfurl_media=False,
            )
            ts = res.get("ts")
            print(f"[slack] posted ok ts={ts}")

            try:
                pres = client.pins_add(channel=channel, timestamp=ts)
                print("[slack] pinned ok:", pres.get("ok"))
            except SlackApiError as e:
                # ピン留めは環境依存で失敗することがあるので、投稿自体は成功として扱う
                print("[slack] pins.add failed:", e.response.get("error"))
                print("[slack] pins.add response:", dict(e.response))

        except SlackApiError as e:
            print("[slack] chat.postMessage failed:", e.response.get("error"), file=sys.stderr)
            print("[slack] response:", dict(e.response), file=sys.stderr)
            return 1

if __name__ == "__main__":
    raise SystemExit(main())