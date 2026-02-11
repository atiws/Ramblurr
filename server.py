import re
import os
import random
import string
import json
import sqlite3
from aiohttp import web

# ==================================================
# SQLite setup
# ==================================================

DB_FILE = "rooms.db"

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# rooms
cur.execute("""
CREATE TABLE IF NOT EXISTS rooms(
    name TEXT PRIMARY KEY,
    private INTEGER
)
""")

# messages
cur.execute("""
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT,
    msg TEXT
)
""")

# users (NEW — persistent usernames)
cur.execute("""
CREATE TABLE IF NOT EXISTS users(
    device TEXT PRIMARY KEY,
    name TEXT
)
""")

conn.commit()


# ==================================================
# DB helpers
# ==================================================

def db_create_room(name, private):
    cur.execute(
        "INSERT OR IGNORE INTO rooms VALUES (?, ?)",
        (name, int(private))
    )
    conn.commit()


def db_add_message(room, msg):
    cur.execute(
        "INSERT INTO messages(room, msg) VALUES (?, ?)",
        (room, msg)
    )
    conn.commit()


def db_get_messages(room, limit=50):
    cur.execute(
        "SELECT msg FROM messages WHERE room=? ORDER BY id DESC LIMIT ?",
        (room, limit)
    )
    return [r["msg"] for r in reversed(cur.fetchall())]


def db_get_user(device):
    cur.execute("SELECT name FROM users WHERE device=?", (device,))
    row = cur.fetchone()
    return row["name"] if row else None


def db_create_user(device, name):
    cur.execute(
        "INSERT INTO users(device, name) VALUES (?, ?)",
        (device, name)
    )
    conn.commit()


def db_get_all_users():
    cur.execute("SELECT name FROM users")
    return [r["name"] for r in cur.fetchall()]


# create global room once
db_create_room("global", False)


# ==================================================
# runtime state (ONLY websocket stuff in RAM)
# ==================================================

rooms = {
    "global": {
        "clients": set(),
        "private": False
    }
}

usernames = {}
user_room = {}
online_users = set()

user_counter = 1

bad_words = {'fuck', 'bitch', 'shit', 'dick', 'nigga', 'nigger', 'wtf'}

MAX_SIZE = 25 * 1024 * 1024


# ==================================================
# helpers
# ==================================================

def make_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def filter_text(msg):
    for bad in bad_words:
        msg = re.sub(rf"\b{bad}\w*\b", "*" * len(bad), msg, flags=re.I)
    return msg


async def broadcast(room, msg):
    db_add_message(room, msg)

    for ws in rooms[room]["clients"]:
        await ws.send_str(msg)


async def send_user_list():
    payload = json.dumps({
        "type": "users",
        "online": list(online_users),
        "all": db_get_all_users()  # persistent users
    })

    for room in rooms.values():
        for ws in room["clients"]:
            await ws.send_str(payload)


# ==================================================
# websocket handler
# ==================================================

async def ws_handler(request):
    global user_counter

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # =====================
    # AUTH
    # =====================

    auth_msg = await ws.receive()
    name = None

    if auth_msg.type == web.WSMsgType.TEXT:
        try:
            data = json.loads(auth_msg.data)

            if data.get("type") == "auth":
                device = data["deviceId"]

                name = db_get_user(device)

                if not name:
                    name = f"Anonymous{user_counter:03d}"
                    db_create_user(device, name)
                    user_counter += 1
        except:
            pass

    if not name:
        name = f"Anonymous{user_counter:03d}"
        user_counter += 1

    usernames[ws] = name
    online_users.add(name)

    # =====================
    # JOIN GLOBAL
    # =====================

    current_room = "global"

    rooms["global"]["clients"].add(ws)
    user_room[ws] = "global"

    for old in db_get_messages("global"):
        await ws.send_str(old)

    await broadcast("global", f"[{name} joined]")
    await send_user_list()

    # =====================
    # MAIN LOOP
    # =====================

    async for msg in ws:

        # IMAGE
        if msg.type == web.WSMsgType.BINARY:
            room = user_room[ws]

            if not rooms[room]["private"]:
                await ws.send_str("[Server]: Images only allowed in private rooms")
                continue

            if len(msg.data) > MAX_SIZE:
                await ws.send_str("[Server]: Image too large (max 25MB)")
                continue

            if not msg.data.startswith(b'\x89PNG') and not msg.data.startswith(b'\xff\xd8'):
                await ws.send_str("[Server]: Only PNG/JPG allowed")
                continue

            for client in rooms[room]["clients"]:
                if client != ws:
                    await client.send_bytes(msg.data)

            continue

        if msg.type != web.WSMsgType.TEXT:
            continue

        text = msg.data.strip()

        # =====================
        # CREATE ROOM
        # =====================

        if text == "/create":
            code = make_code()

            if code not in rooms:
                rooms[code] = {
                    "clients": set(),
                    "private": True
                }

                db_create_room(code, True)

            rooms[current_room]["clients"].discard(ws)

            current_room = code
            user_room[ws] = code
            rooms[code]["clients"].add(ws)

            await ws.send_str(f"[Room created] Code: {code}")
            await send_user_list()
            continue

        # =====================
        # JOIN ROOM
        # =====================

        if text.startswith("/join "):
            code = text.split(" ", 1)[1]

            if code not in rooms:
                await ws.send_str("[Room not found]")
                continue

            rooms[current_room]["clients"].discard(ws)

            current_room = code
            user_room[ws] = code
            rooms[code]["clients"].add(ws)

            for old in db_get_messages(code):
                await ws.send_str(old)

            await broadcast(code, f"[{name} joined]")
            await send_user_list()
            continue

        # =====================
        # NORMAL MESSAGE
        # =====================

        clean = filter_text(text) if current_room == "global" else text
        await broadcast(current_room, f"{name}: {clean}")

    # =====================
    # DISCONNECT
    # =====================

    rooms[current_room]["clients"].discard(ws)
    user_room.pop(ws, None)

    await broadcast(current_room, f"[{name} left]")

    usernames.pop(ws, None)
    online_users.discard(name)

    await send_user_list()

    return ws


# ==================================================
# app setup
# ==================================================

app = web.Application()
app.router.add_get("/ws", ws_handler)
app.router.add_static("/static/", "./static", show_index=False)


async def home(request):
    return web.FileResponse("client.html")


app.router.add_get("/", home)

web.run_app(app, port=int(os.environ.get("PORT", 8000)))
