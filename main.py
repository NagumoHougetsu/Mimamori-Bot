import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

import requests
from fastapi import FastAPI, HTTPException, Request
from google.cloud import firestore

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

# =========================================================
# 重要：LINE署名検証を「必須にしない」版
#  - envは LINE_TOKEN / GROUP_ID だけでも動く
#  - LINE_CHANNEL_SECRET が設定されている場合だけ署名検証する
# =========================================================

# -----------------------------
# Env (互換を吸収)
# -----------------------------
LINE_CHANNEL_SECRET = (
    os.environ.get("LINE_CHANNEL_SECRET")
    or os.environ.get("LINE_SECRET")
    or ""
)

# アクセストークンは旧名 LINE_TOKEN も許容
LINE_CHANNEL_ACCESS_TOKEN = (
    os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    or os.environ.get("LINE_TOKEN")
    or ""
)

# グループIDも旧名 GROUP_ID を許容
GROUP_ID_DEFAULT = (
    os.environ.get("LINE_GROUP_ID")
    or os.environ.get("GROUP_ID")
    or ""
)

if not LINE_CHANNEL_ACCESS_TOKEN:
    print("[WARN] LINE access token is empty (LINE_CHANNEL_ACCESS_TOKEN / LINE_TOKEN)")

# -----------------------------
# Firestore
# -----------------------------
db = firestore.Client()
COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "watchdog")
DOC_ID = os.environ.get("FIRESTORE_DOC_ID", "settings")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "group_id": GROUP_ID_DEFAULT,
    "interval_min": 15,       # 通知間隔（同じアラート連投防止）
    "volume_th": 70,          # 着信音のしきい値（%）
    "paused": False,          # Trueなら通知停止
    "last_alert_at": "",      # 最後にアラート通知した時刻（ISO）
    "updated_at": "",         # 設定更新時刻（ISO）

    # ★音量操作：MacroDroidが適用する「希望値」
    "desired_vol_ring": None,   # Optional[int]
    "desired_vol_notif": None,  # Optional[int]
    "desired_ringer_mode": None,  # Optional[int] 0=silent 1=vibrate 2=normal（機種差あり）

    "last_status": {
        "vol_ring": None,     # 着信音
        "vol_notif": None,    # 通知音
        "ringer_mode": None,  # 着信モード（0/1/2 想定）
        "battery": None,      # 電池残量(%)
        "updated_at": "",
    },
}


def _now_jst() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Tokyo"))
    return datetime.now().astimezone()


def now_iso() -> str:
    """JSTでISO文字列を返す（例: 2025-12-20T19:58:00+09:00）"""
    return _now_jst().isoformat(timespec="seconds")


def load_settings() -> Dict[str, Any]:
    ref = db.collection(COLLECTION).document(DOC_ID)
    doc = ref.get()
    if not doc.exists:
        s = DEFAULT_SETTINGS.copy()
        s["updated_at"] = now_iso()
        ref.set(s)
        return s

    s = doc.to_dict() or {}
    merged = DEFAULT_SETTINGS.copy()

    # shallow merge
    for k, v in s.items():
        merged[k] = v

    # nested merge for last_status
    ls = DEFAULT_SETTINGS["last_status"].copy()
    ls.update(merged.get("last_status") or {})
    merged["last_status"] = ls

    return merged


def save_settings(s: Dict[str, Any]) -> None:
    s["updated_at"] = now_iso()
    db.collection(COLLECTION).document(DOC_ID).set(s)


# -----------------------------
# LINE Messaging API (simple)
# -----------------------------
def _line_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def reply(reply_token: str, message: Dict[str, Any]) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    payload = {"replyToken": reply_token, "messages": [message]}
    r = requests.post(url, headers=_line_headers(), data=json.dumps(payload))
    if r.status_code >= 400:
        print("[LINE reply ERROR]", r.status_code, r.text)


def push(to: str, message: Dict[str, Any]) -> None:
    url = "https://api.line.me/v2/bot/message/push"
    payload = {"to": to, "messages": [message]}
    r = requests.post(url, headers=_line_headers(), data=json.dumps(payload))
    if r.status_code >= 400:
        print("[LINE push ERROR]", r.status_code, r.text)


# -----------------------------
# Flex Helpers
# -----------------------------
def _data(cmd: str, v: Optional[int] = None) -> str:
    if v is None:
        return f"cmd={cmd}"
    return f"cmd={cmd}&v={v}"


def parse_postback(data: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in data.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def _btn_postback(label: str, data: str, selected: bool) -> Dict[str, Any]:
    b: Dict[str, Any] = {
        "type": "button",
        "height": "sm",
        "style": "primary" if selected else "secondary",
        "action": {"type": "postback", "label": label, "data": data},
    }
    if selected:
        b["color"] = "#06C755"
    return b


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        pass

    # "70%", "70.0", "vol=70" みたいなのを救う
    try:
        s = str(v).strip()
        m = re.search(r"-?\d+(\.\d+)?", s)
        if not m:
            return None
        return int(round(float(m.group(0))))
    except Exception:
        return None


def settings_ui_contents(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    interval_min = int(s.get("interval_min", 15))
    volume_th = int(s.get("volume_th", 70))
    paused = bool(s.get("paused", False))

    desired_ring = _to_int(s.get("desired_vol_ring"))
    desired_notif = _to_int(s.get("desired_vol_notif"))
    desired_ringer = _to_int(s.get("desired_ringer_mode"))

    def two_rows(values: List[int], cmd: str, selected_fn, label_fn) -> List[Dict[str, Any]]:
        row1 = values[:2]
        row2 = values[2:4]
        return [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [_btn_postback(label_fn(v), _data(cmd, v), selected_fn(v)) for v in row1],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [_btn_postback(label_fn(v), _data(cmd, v), selected_fn(v)) for v in row2],
            },
        ]

    contents: List[Dict[str, Any]] = []

    # 通知間隔
    contents.append({"type": "text", "text": "通知間隔", "weight": "bold", "size": "sm"})
    contents.extend(two_rows([10, 15, 30, 60], "set_interval", lambda v: v == interval_min, lambda v: f"{v}分"))

    # 音量基準（判定）
    contents.append({"type": "text", "text": "音量基準", "weight": "bold", "size": "sm", "margin": "md"})
    contents.extend(two_rows([30, 50, 70, 90], "set_volume", lambda v: v == volume_th, lambda v: f"{v}%"))

    # 通知状況
    contents.append({"type": "text", "text": "通知状況", "weight": "bold", "size": "sm", "margin": "md"})
    contents.append(
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                _btn_postback("ON", _data("resume"), selected=not paused),
                _btn_postback("OFF", _data("pause"), selected=paused),
            ],
        }
    )

    # ★音量設定（MacroDroidが反映）
    # 要望：通知音と着信音を同じ数値でいじれればOK → まとめてセットするボタンにする
    contents.append({"type": "text", "text": "音量設定（端末へ指示）", "weight": "bold", "size": "sm", "margin": "md"})

    # 表示上の選択状態：ring / notif の両方が同じ値ならその値をハイライト
    selected_50 = (desired_ring == 50 and desired_notif == 50)
    selected_100 = (desired_ring == 100 and desired_notif == 100)

    contents.append(
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                _btn_postback("50%", _data("set_both_volume", 50), selected=selected_50),
                _btn_postback("100%", _data("set_both_volume", 100), selected=selected_100),
            ],
        }
    )

    # 情報取得
    contents.append(
        {
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "margin": "md",
            "action": {"type": "postback", "label": "情報取得", "data": _data("get_info")},
        }
    )

    contents.append(
        {"type": "text", "text": f"更新(JST)：{now_iso()}", "size": "xxs", "color": "#888888", "wrap": True, "margin": "sm"}
    )
    contents.append({"type": "text", "text": "※消しても /panel で再表示", "size": "xxs", "color": "#888888", "wrap": True})

    return contents


def flex_settings_notice(s: Dict[str, Any], headline: str = "🧭 見守り操作パネル") -> Dict[str, Any]:
    body_contents: List[Dict[str, Any]] = [
        {"type": "text", "text": headline, "weight": "bold", "size": "md"},
        {"type": "separator", "margin": "md"},
    ]
    body_contents.extend(settings_ui_contents(s))

    return {
        "type": "flex",
        "altText": "見守り操作パネル",
        "contents": {
            "type": "bubble",
            "styles": _bubble_styles("#F6F6F6"),  # ← パネルの座布団色
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
        },
    }

def flex_panel(s: Dict[str, Any]) -> Dict[str, Any]:
    return flex_settings_notice(s)


def flex_event_notice(
    s: Dict[str, Any],
    vol_ring: Optional[int],
    vol_notif: Optional[int],
    battery: Optional[int],
    ringer_mode: Optional[int] = None,
    attach_settings_ui: bool = True,
    status_updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    volume_th = int(s.get("volume_th", 70))

    ring_low = False
    ring_blocked = False

    # ringer_mode（0=silent / 1=vibrate / 2=normal 想定。未取得ならNone）
    rm = _to_int(ringer_mode)
    if rm is not None and int(rm) in (0, 1):
        ring_blocked = True
        sound_state = "サイレント" if int(rm) == 0 else "バイブ"
    else:
        if vol_ring is None:
            sound_state = "未取得"
        else:
            ring_low = int(vol_ring) < volume_th
            sound_state = "着信音低下" if ring_low else "問題なし"

    ring_text = "未取得" if vol_ring is None else f"{int(vol_ring)}%"

    notif_text = "未取得" if vol_notif is None else f"{int(vol_notif)}%"
    batt_text = "未取得" if battery is None else f"{int(battery)}%"

    header = "📣 状態通知"
    if ring_blocked:
        header = "🔕 消音モード"
    elif ring_low:
        header = "🔔 着信音量低下"

    if not status_updated_at:
        status_updated_at = s.get("last_status", {}).get("updated_at") or now_iso()

    body: List[Dict[str, Any]] = [
        {"type": "text", "text": header, "weight": "bold", "size": "md"},
        {"type": "text", "text": f"音量状態：{sound_state}", "size": "sm", "wrap": True},
        {"type": "text", "text": f"着信音量：{ring_text}", "size": "sm", "wrap": True},
        {"type": "text", "text": f"通知音量：{notif_text}", "size": "sm", "wrap": True},
        {"type": "text", "text": f"電池残量：{batt_text}", "size": "sm", "wrap": True},
    ]

    body.append({"type": "text", "text": f"状態更新(JST)：{status_updated_at}", "size": "xxs", "color": "#888888", "wrap": True})

    if attach_settings_ui:
        body.append({"type": "separator", "margin": "md"})
        body.extend(settings_ui_contents(s))

    # ★ここが座布団色の決定ロジック
    bg = "#FFFFFF"          # 通常
    if (ring_low or ring_blocked):
        bg = "#FFF8E1"      # 薄黄（注意）

    return {
        "type": "flex",
        "altText": header,
        "contents": {
            "type": "bubble",
            "styles": _bubble_styles(bg),   # ←座布団色
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
        },
    }


def _bubble_styles(bg: str) -> Dict[str, Any]:
    # Flexの“座布団”色（bubble内の背景色）を指定
    return {
        "body": {
            "backgroundColor": bg
        }
    }

# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI()


def _verify_line_signature_if_configured(body: bytes, signature: str) -> bool:
    """Channel Secret がある時だけ検証する。

    - Secret未設定なら True（=検証スキップ）
    - Secret設定済みなら通常の署名検証
    """
    if not LINE_CHANNEL_SECRET:
        return True

    signature = (signature or "").strip()
    if not signature:
        return False

    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    # Secretが入ってる場合のみ、署名がNGなら400
    if LINE_CHANNEL_SECRET and (not _verify_line_signature_if_configured(body, signature)):
        raise HTTPException(status_code=400, detail="Bad signature")

    payload = json.loads(body.decode("utf-8"))
    events = payload.get("events", [])
    s = load_settings()

    for ev in events:
        typ = ev.get("type")
        reply_token = ev.get("replyToken", "")

        if typ == "message" and ev.get("message", {}).get("type") == "text":
            text = (ev.get("message", {}).get("text") or "").strip()

            if text == "/panel":
                reply(reply_token, flex_panel(s))
            elif text == "/status":
                last = s.get("last_status") or {}
                vol_ring = _to_int(last.get("vol_ring"))
                vol_notif = _to_int(last.get("vol_notif") or last.get("volume_percent"))  # 互換
                battery = _to_int(last.get("battery"))
                ringer_mode = _to_int(last.get("ringer_mode"))
                updated_at = last.get("updated_at")
                reply(
                    reply_token,
                    flex_event_notice(s, vol_ring, vol_notif, battery, ringer_mode=ringer_mode, attach_settings_ui=True, status_updated_at=updated_at),
                )
            else:
                reply(
                    reply_token,
                    {
                        "type": "text",
                        "text": "コマンド:\n/panel … 設定パネル\n/status … 現在状態\n（通知は着信音低下などの注意時に飛びます）",
                    },
                )

        elif typ == "postback":
            data = ev.get("postback", {}).get("data", "")
            pb = parse_postback(data)
            cmd = pb.get("cmd", "")
            v = pb.get("v")

            if cmd == "set_interval" and v:
                s["interval_min"] = int(v)
                save_settings(s)
                reply(reply_token, flex_panel(s))

            elif cmd == "set_volume" and v:
                s["volume_th"] = int(v)
                save_settings(s)
                reply(reply_token, flex_panel(s))

            elif cmd == "pause":
                s["paused"] = True
                save_settings(s)
                reply(reply_token, flex_panel(s))

            elif cmd == "resume":
                s["paused"] = False
                save_settings(s)
                reply(reply_token, flex_panel(s))

            # ★音量指示：両方同値
            elif cmd == "set_both_volume" and v:
                vv = int(v)
                s["desired_vol_ring"] = vv
                s["desired_vol_notif"] = vv
                s["desired_ringer_mode"] = 2  # 音量指示時はサイレント/バイブを解除したい
                save_settings(s)
                # パネル更新（選択状態が反映される）
                reply(reply_token, flex_panel(s))

            elif cmd == "get_info":
                last = s.get("last_status") or {}
                vol_ring = _to_int(last.get("vol_ring"))
                vol_notif = _to_int(last.get("vol_notif") or last.get("volume_percent"))
                battery = _to_int(last.get("battery"))
                ringer_mode = _to_int(last.get("ringer_mode"))
                updated_at = last.get("updated_at")
                flex = flex_event_notice(s, vol_ring, vol_notif, battery, ringer_mode=ringer_mode, attach_settings_ui=True, status_updated_at=updated_at)
                reply(reply_token, flex)

            else:
                reply(reply_token, {"type": "text", "text": f"unknown cmd: {cmd}"})

    return {"ok": True}


@app.post("/gps")
async def gps_event(request: Request):
    """MacroDroidなどから送られてくる状態更新を受け取る。"""
    data = await request.json()
    s = load_settings()

    # ===== 部分更新（未送信の項目で None 上書きしない）=====
    prev = (s.get("last_status") or {}).copy()

    # 音量＆電池：取れた時だけ更新（取れなければ維持）
    vol_ring_new = _to_int(data.get("vol_ring") or data.get("vol_ring_percent") or data.get("volume_ring_percent") or data.get("volume_ring") or data.get("ring_volume") or data.get("vol_ringtone"))
    if vol_ring_new is not None:
        prev["vol_ring"] = vol_ring_new
    vol_ring = _to_int(prev.get("vol_ring"))
    
    vol_notif_new = _to_int(data.get("vol_notif") or data.get("vol_notif_percent") or data.get("volume_notif_percent") or data.get("volume_notif") or data.get("notif_volume"))
    if vol_notif_new is not None:
        prev["vol_notif"] = vol_notif_new
    vol_notif = _to_int(prev.get("vol_notif"))

    battery_new = _to_int(data.get("battery") or data.get("battery_level") or data.get("battery_percent"))
    if battery_new is not None:
        prev["battery"] = battery_new
    battery = _to_int(prev.get("battery"))

    # 着信モード：取れた時だけ更新（取れなければ維持）
    ringer_mode_new = _to_int(
        data.get("ringer_mode")
        or data.get("mode_ringer")
        or data.get("ringer_mode_global")
        or data.get("setting_global_mode_ringer")
    )
    if ringer_mode_new is not None:
        prev["ringer_mode"] = ringer_mode_new
    ringer_mode = _to_int(prev.get("ringer_mode"))

    prev["updated_at"] = now_iso()

    status = prev
    s["last_status"] = status
    # ===== ここまで部分更新 =====

    # -----------------------------
    # ★希望音量の自動クリア
    # 端末が希望値に到達していたら、もう指示しない
    # -----------------------------
    desired_ring = _to_int(s.get("desired_vol_ring"))
    desired_notif = _to_int(s.get("desired_vol_notif"))
    desired_ringer = _to_int(s.get("desired_ringer_mode"))

    if desired_ring is not None and vol_ring is not None and int(vol_ring) == int(desired_ring):
        s["desired_vol_ring"] = None
        desired_ring = None

    if desired_notif is not None and vol_notif is not None and int(vol_notif) == int(desired_notif):
        s["desired_vol_notif"] = None
        desired_notif = None

    if desired_ringer is not None and ringer_mode is not None and int(ringer_mode) == int(desired_ringer):
        s["desired_ringer_mode"] = None
        desired_ringer = None

    save_settings(s)

    # -----------------------------
    # 通知判定
    # -----------------------------
    volume_th = int(s.get("volume_th", 70))
    ring_low = (vol_ring is not None) and (int(vol_ring) < volume_th)
    ring_blocked = (ringer_mode is not None) and (int(ringer_mode) in (0, 1))
    
    alert_needed = ring_low or ring_blocked

    # /gps のレスポンスは MacroDroid が使う（希望音量を返す）
    resp_base = {
        "ok": True,
        "sent": False,
        "reason": "ok",
        "desired_vol_ring": desired_ring,
        "desired_vol_notif": desired_notif,
        "desired_ringer_mode": desired_ringer,
    }

    if not alert_needed:
        return resp_base

    if bool(s.get("paused", False)):
        resp_base["reason"] = "paused"
        return resp_base

    interval_min = int(s.get("interval_min", 15))
    last_alert_at = s.get("last_alert_at")
    if isinstance(last_alert_at, str) and last_alert_at:
        try:
            last_dt = datetime.fromisoformat(last_alert_at.replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now_iso())
            if (now_dt - last_dt) < timedelta(minutes=interval_min):
                resp_base["reason"] = "interval"
                return resp_base
        except Exception:
            pass

    # group_id は Firestore が空文字でも env を救済する
    group_id = (s.get("group_id") or GROUP_ID_DEFAULT or "").strip()
    if not group_id:
        return {"ok": False, "sent": False, "reason": "group_id_missing"}

    flex = flex_event_notice(
        s=s,
        vol_ring=vol_ring,
        vol_notif=vol_notif,
        battery=battery,
        ringer_mode=ringer_mode,
        attach_settings_ui=True,
        status_updated_at=status.get("updated_at"),
    )
    push(group_id, flex)

    s["last_alert_at"] = now_iso()
    save_settings(s)

    return {
        "ok": True,
        "sent": True,
        "desired_vol_ring": desired_ring,
        "desired_vol_notif": desired_notif,
        "desired_ringer_mode": desired_ringer,
    }
