"""只读访问白名单里的微信群。

库的结构：联系人在 contact.db 的 contact（好友）和 stranger（非好友）表，群昵称在 chat_room.ext_buffer（protobuf）；
消息在 message_0.db、message_1.db…… 里按会话分表，表名是 Msg_ 加群 id 的 md5，较长的内容用 zstd 压缩；
消息的 source 列是一段 XML，其中 atuserlist 记着这条消息 @ 了谁。
群里收到的文件由微信存在 <账号目录>/msg/file/<年-月>/ 下，重名时文件名后加 (1)、(2)。
"""

import hashlib
import html
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import zstandard
from filelock import FileLock

from . import decrypt
from .config import DATA

KINDS = {3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情", 48: "位置", 50: "通话"}
APP_KINDS = {5: "链接", 6: "文件", 19: "聊天记录", 33: "小程序", 36: "小程序", 51: "视频号", 53: "接龙", 2000: "转账"}
RECORD_KINDS = {2: "图片", 3: "语音", 4: "视频", 6: "位置", 7: "链接", 16: "名片", 22: "视频号"}
MAX_TEXT = 1000      # 一条普通消息最多显示的字数
MAX_RECORD = 4000    # 一条合并转发的聊天记录展开后最多显示的字数
_zstd = zstandard.ZstdDecompressor()
_lock = FileLock(str(DATA / ".lock"), timeout=120)


class Unavailable(Exception):
    pass


def _open(d: Path, name: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{(d / name).as_posix()}?mode=ro&immutable=1", uri=True)


def _decode(compressed, value) -> str:
    if isinstance(value, bytes):
        if compressed == 4:
            try:
                value = _zstd.decompress(value)
            except zstandard.ZstdError:
                return ""
        value = value.decode("utf-8", "replace")
    return value or ""


def _xml(text: str):
    try:
        return ET.fromstring(text[text.index("<"):])
    except (ValueError, ET.ParseError):
        return None


# ---------- 名字 ----------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    out = shift = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, i
        shift += 7


def _proto(buf: bytes):
    """逐个产出 protobuf 的（字段号，值），只处理 varint 和长度前缀两种类型。"""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        else:
            return
        yield field, value


def _group_nicks(con: sqlite3.Connection, room: str) -> dict[str, str]:
    """群成员 id -> 在这个群里的群昵称（没设群昵称的不在里面）。"""
    row = con.execute("SELECT ext_buffer FROM chat_room WHERE username=?", (room,)).fetchone()
    out = {}
    try:
        for field, member in _proto(row[0] if row and row[0] else b""):
            if field != 1 or not isinstance(member, bytes):
                continue
            parts = dict(_proto(member))
            if parts.get(1) and parts.get(2):
                out[parts[1].decode("utf-8", "replace")] = parts[2].decode("utf-8", "replace")
    except (IndexError, ValueError):
        pass
    return out


class Names:
    """显示名：备注 > 群昵称 > 微信昵称 > id。"""

    def __init__(self, d: Path, rooms: list[str]):
        con = _open(d, "contact.db")
        self.remark, self.nick = {}, {}
        for table in ("stranger", "contact"):  # 好友表后读，覆盖陌生人表里的旧信息
            for u, remark, nick in con.execute(f"SELECT username, remark, nick_name FROM {table}"):
                if remark:
                    self.remark[u] = remark
                if nick:
                    self.nick[u] = nick
        self.group = {r: _group_nicks(con, r) for r in rooms}
        con.close()

    def of(self, user: str, room: str = "") -> str:
        return self.remark.get(user) or self.group.get(room, {}).get(user) or self.nick.get(user) or user


def all_groups(d: Path) -> list[tuple[str, str]]:
    """本机微信里所有的群（群名，id），给配置白名单时查 id 用。"""
    con = _open(d, "contact.db")
    rows = con.execute("SELECT username, remark, nick_name FROM contact WHERE username LIKE '%@chatroom'").fetchall()
    con.close()
    return sorted(((remark or nick or u), u) for u, remark, nick in rows)


# ---------- 文件 ----------

class Files:
    """按文件名和大小在 msg/file/ 下找微信已经下载到本机的文件。"""

    def __init__(self, storage: Path):
        self.root = storage.parent / "msg" / "file"
        self._dirs: dict[str, list[Path]] = {}

    def _list(self, month: str) -> list[Path]:
        if month not in self._dirs:
            d = self.root / month
            self._dirs[month] = list(d.iterdir()) if d.is_dir() else []
        return self._dirs[month]

    def find(self, title: str, size: int, ts: float) -> tuple[Path | None, bool]:
        """返回（路径，大小是否对得上）。先找消息所在月份和下个月的目录，找不到再找全部目录。"""
        if not title:
            return None, False
        stem, dot, ext = title.rpartition(".")
        if not dot:
            stem, ext = title, ""
        pattern = re.compile(re.escape(stem) + r"(\(\d+\))?" + (re.escape("." + ext) if ext else "") + "$")
        t = datetime.fromtimestamp(ts) if ts else datetime.now()
        near = [f"{t:%Y-%m}", f"{(t.replace(day=1) + timedelta(days=32)):%Y-%m}"]
        rest = sorted((p.name for p in self.root.iterdir() if p.is_dir()), reverse=True) if self.root.is_dir() else []
        loose = None
        for month in near + [m for m in rest if m not in near]:
            for p in self._list(month):
                if not pattern.fullmatch(p.name):
                    continue
                if size and p.stat().st_size == size:
                    return p, True
                if loose is None and p.name == title:
                    loose = p
        return loose, loose is not None and not size

    def describe(self, title: str, size: int, ts: float) -> str:
        path, exact = self.find(title, size, ts)
        size_text = f"，{_human(size)}" if size else ""
        if path is None:
            return f"[文件] {title}（未下载到本机{size_text}，需要先在微信里点开下载）"
        warn = "" if exact or not size else "，大小和消息里记的不一致，可能不是同一份"
        return f"[文件] {title}（本地：{path}{size_text}{warn}）"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# ---------- 把一条消息变成文字 ----------

@dataclass
class Msg:
    ts: float
    room: str
    sender: str      # 发送者的微信 id，系统消息为空或等于群 id
    kind: int
    content: str     # 已解压，发送者前缀已去掉
    at: list[str]    # 这条消息 @ 的 id，@所有人 为 notify@all
    text: str = ""
    files: list[tuple[str, int, float]] | None = None  # 消息里带的文件（文件名，大小，原消息时间）


def _record(info, files: Files, refs: list, depth: int = 0) -> list[str]:
    """展开合并转发的聊天记录，每条一行；嵌套的聊天记录缩进一层继续展开。"""
    lines = []
    pad = "    " * (depth + 1)
    for item in info.findall("datalist/dataitem"):
        kind = int(item.get("datatype") or 0)
        who = item.findtext("sourcename") or ""
        when = (item.findtext("sourcetime") or "").strip()
        when = when[5:16] if len(when) >= 16 else when
        title = (item.findtext("datatitle") or "").strip()
        desc = (item.findtext("datadesc") or "").strip()
        ts = float(item.findtext("srcMsgCreateTime") or 0)
        if kind == 1:
            body = desc
        elif kind == 8:
            size = int(item.findtext("datasize") or 0)
            refs.append((title, size, ts))
            body = files.describe(title, size, ts)
        elif kind == 17:
            inner = item.find("recordxml/recordinfo")
            if inner is None:
                inner = _xml(item.findtext("recordxml") or "")
            body = f"[聊天记录] {title or desc}"
            lines.append(f"{pad}{when} {who}：{body}")
            if inner is not None:
                lines += _record(inner, files, refs, depth + 1)
            continue
        else:
            label = RECORD_KINDS.get(kind, f"类型 {kind}")
            body = f"[{label}]" + (f" {title or desc}" if title or desc else "")
        lines.append(f"{pad}{when} {who}：{re.sub(r'\s+', ' ', body)}")
    return lines


def _render(m: Msg, files: Files):
    """填好 m.text 和 m.files。"""
    m.files = []
    kind, content = m.kind, m.content
    if kind == 1:
        m.text = content[:MAX_TEXT]
    elif kind == 49:
        m.text = _render_app(content, files, m)
    elif kind == 10000:
        m.text = _render_system(content)
    else:
        m.text = f"[{KINDS.get(kind, f'类型 {kind}')}]"


def _render_app(content: str, files: Files, m: Msg) -> str:
    root = _xml(content)
    app = root.find("appmsg") if root is not None else None
    if app is None:
        return "[链接或文件]"
    title = (app.findtext("title") or "").strip()
    sub = int(app.findtext("type") or 0)
    if sub == 57:  # 引用回复
        ref = app.find("refermsg")
        if ref is None:
            return title
        who = ref.findtext("displayname") or ""
        quoted = html.unescape(ref.findtext("content") or "")
        if ref.findtext("type") != "1" or quoted.lstrip().startswith("<"):
            inner = _xml(quoted)
            inner_title = inner.findtext("appmsg/title") if inner is not None else None
            quoted = f"[{inner_title}]" if inner_title else "[非文字消息]"
        quoted = re.sub(r"\s+", " ", quoted)
        return f"{title[:MAX_TEXT]}（回复 {who}：{quoted[:80]}）"
    if sub == 6:
        size = int(app.findtext("appattach/totallen") or 0)
        m.files.append((title, size, m.ts))
        return files.describe(title, size, m.ts)
    if sub == 19:
        info = _xml(app.findtext("recorditem") or "")
        if info is None:
            return f"[聊天记录] {title}：{(app.findtext('des') or '')[:300]}"
        body = "\n".join(_record(info, files, m.files))
        if len(body) > MAX_RECORD:
            body = body[:MAX_RECORD] + "\n    ……（后面还有，太长截断）"
        return f"[聊天记录] {title}\n{body}"
    label = APP_KINDS.get(sub, "分享")
    des = re.sub(r"\s+", " ", (app.findtext("des") or "").strip())
    if sub == 53 and des:
        return f"[{label}] {title}：{des[:MAX_TEXT]}"
    url = (app.findtext("url") or "").strip() if sub == 5 else ""
    return f"[{label}] {title}" + (f" {url}" if url else "")


def _render_system(content: str) -> str:
    """撤回、进群、改群名、置顶等系统提示。"""
    root = _xml(content)
    if root is None:
        return f"[系统] {re.sub(r'<[^>]+>', '', content).strip()}"
    if root.get("type") == "revokemsg":
        return f"[系统] {(root.findtext('.//content') or '有人撤回了一条消息').strip()}"
    if root.get("type") == "mmchatroomtopmsg":
        return f"[系统] {root.findtext('.//nickname') or '有人'} 修改了群置顶消息"
    tmpl = root.findtext(".//template") or ""
    for link in root.iter("link"):
        names = [x.findtext("nickname") or "" for x in link.iter("member")]
        value = "、".join(n for n in names if n) or (link.findtext("plain") or "")
        tmpl = tmpl.replace(f"${link.get('name')}$", value)
    return f"[系统] {tmpl.strip() or '（无法解析）'}"


# ---------- 时间 ----------

_UNITS = {"m": 60, "min": 60, "分钟": 60, "h": 3600, "小时": 3600, "d": 86400, "天": 86400, "w": 604800, "周": 604800}


def parse_time(s: str) -> datetime:
    """绝对时间（YYYY-MM-DD [HH:MM]、MM-DD）、日期词（今天、昨天、前天、本周、上周，表示那天 0 点）、
    或时长（30m、3h、2d、1w，表示现在往前推这么久）。"""
    s = s.strip().lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    words = {"今天": today, "today": today, "昨天": today - timedelta(days=1), "yesterday": today - timedelta(days=1),
             "前天": today - timedelta(days=2), "本周": today - timedelta(days=today.weekday()),
             "上周": today - timedelta(days=today.weekday() + 7)}
    if s in words:
        return words[s]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(min|m|分钟|h|小时|d|天|w|周)前?", s)
    if m:
        return now - timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            t = datetime.strptime(s, fmt)
            return t if "%Y" in fmt else t.replace(year=now.year)
        except ValueError:
            continue
    raise Unavailable(f"看不懂时间“{s}”。可以用 2026-11-20、2026-11-20 14:00、今天、昨天、本周、3h、2d 这类写法")


# ---------- 对外接口 ----------

class Reader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rooms: list[str] = [g["id"] for g in cfg.get("groups", [])]
        if not self.rooms:
            raise Unavailable("config.toml 里没有配置任何群")

    def _match(self, names: Names, group: str) -> list[str]:
        """group 为空时返回全部白名单群；否则按 id 或名字子串在白名单里找。"""
        if not group:
            return self.rooms
        g = group.strip().lower()
        hits = [r for r in self.rooms if r == g] or [r for r in self.rooms if g in names.of(r).lower()]
        if not hits:
            allowed = "、".join(names.of(r) for r in self.rooms)
            raise Unavailable(f"“{group}”不在可读的群里。可读的群：{allowed}")
        return hits

    @staticmethod
    def _load(d: Path, room: str, since: float, until: float) -> list[Msg]:
        table = "Msg_" + hashlib.md5(room.encode()).hexdigest()
        out = []  # 一个群的消息可能分散在几个 message 库里，发送者编号是每个库各自的
        for f in sorted(d.glob("message_[0-9]*.db")):
            con = _open(d, f.name)
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                senders = dict(con.execute("SELECT rowid, user_name FROM Name2Id"))
                rows = con.execute(
                    f'SELECT create_time, real_sender_id, local_type, WCDB_CT_message_content, message_content, '
                    f'WCDB_CT_source, source FROM "{table}" WHERE create_time >= ? AND create_time < ?',
                    (since, until))
                for ts, s, kind, cc, content, cs, source in rows:
                    text = re.sub(r"^[A-Za-z0-9_@.-]+:\n", "", _decode(cc, content))  # 群消息正文前的发送者 id
                    at = re.search(r"<atuserlist>(.*?)</atuserlist>", _decode(cs, source), re.S)
                    at = [x.strip() for x in at.group(1).split(",") if x.strip()] if at else []
                    out.append(Msg(ts, room, senders.get(s, ""), kind & 0xFFFFFFFF, text, at))
            con.close()
        out.sort(key=lambda m: m.ts)
        return out

    def _collect(self, group: str, start: datetime, end: datetime):
        """在锁里刷新解密副本并读出消息。返回（说明，自己的 id，名字表，涉及的群，各群的消息）。"""
        with _lock:
            d, me, note, storage = decrypt.refresh(self.cfg)
            names = Names(d, self.rooms)
            rooms = self._match(names, group)
            msgs = {r: self._load(d, r, start.timestamp(), end.timestamp()) for r in rooms}
        self._files = Files(storage)
        for ms in msgs.values():
            for m in ms:
                _render(m, self._files)
        return note, me, names, rooms, msgs

    def groups(self) -> str:
        with _lock:
            d, me, note, _ = decrypt.refresh(self.cfg)
            names = Names(d, self.rooms)
            week = (datetime.now() - timedelta(days=7)).timestamp()
            lines = []
            for r in self.rooms:
                ms = self._load(d, r, 0, 1e12)
                when = f"最后一条 {datetime.fromtimestamp(ms[-1].ts):%Y-%m-%d %H:%M}" if ms else "没有消息"
                recent = sum(1 for m in ms if m.ts >= week)
                lines.append(f"{names.of(r)}（{r}）：{when}，最近 7 天 {recent} 条，共 {len(ms)} 条")
        return note + "\n".join(lines)

    def messages(self, group: str = "", days: float | None = None, since: str = "", until: str = "",
                 query: str = "", sender: str = "", mentions: bool = False, context: int = 2,
                 limit: int = 300) -> str:
        filtering = bool(query or sender or mentions)
        if since:
            start = parse_time(since)
        else:  # 不指定时间时，浏览默认看 3 天，搜索默认搜全部历史
            start = datetime.now() - timedelta(days=days if days is not None else (3650 if filtering else 3))
        end = parse_time(until) if until else datetime.now() + timedelta(days=1)
        note, me, names, rooms, msgs = self._collect(group, start, end)

        def label(m: Msg) -> str:
            if m.sender == me:
                return "我"
            return "系统" if not m.sender or m.sender == m.room else names.of(m.sender, m.room)

        def hit(m: Msg) -> bool:
            if query and query.lower() not in m.text.lower():
                return False
            if sender and sender.lower() not in label(m).lower():
                return False
            return not mentions or me in m.at or "notify@all" in m.at

        multi = len(rooms) > 1

        def line(m: Msg, mark: str = "") -> str:
            tag = f"[{names.of(m.room)}] " if multi else ""
            return f"{mark}{datetime.fromtimestamp(m.ts):%m-%d %H:%M} {tag}{label(m)}：{m.text}"

        scope = names.of(rooms[0]) if not multi else f"{len(rooms)} 个群"
        conds = [f"含“{query}”" if query else "", f"发送者含“{sender}”" if sender else "",
                 "@我或@所有人" if mentions else ""]
        cond = "、".join(c for c in conds if c)
        span = "全部历史中" if filtering and not since and days is None else f"{start:%Y-%m-%d %H:%M} 起"
        head = f"{scope}，{span}" + (f"{cond}的" if cond else "的")

        if not filtering:
            flat = sorted((m for ms in msgs.values() for m in ms), key=lambda m: m.ts)
            cut = len(flat) > limit
            flat = flat[-limit:]
            head += f"消息 {len(flat)} 条，旧的在前"
            if cut:
                head += f"（超过 {limit} 条，只保留最新的；要看更早的用 until 往前翻）"
            return note + head + "。\n" + "\n".join(line(m) for m in flat)

        hits = sorted(((r, i) for r, ms in msgs.items() for i, m in enumerate(ms) if hit(m)),
                      key=lambda x: msgs[x[0]][x[1]].ts)
        cut = len(hits) > limit
        hits = hits[-limit:]
        head += f"消息 {len(hits)} 条，旧的在前"
        if cut:
            head += f"（超过 {limit} 条，只保留最新的；要看更早的用 until 往前翻）"
        if not hits:
            return note + head + "。"
        if context <= 0:
            return note + head + "。\n" + "\n".join(line(msgs[r][i]) for r, i in hits)
        # 每条命中带上同一个群里前后各 context 条，相邻或重叠的合成一段
        blocks: list[tuple[str, int, int]] = []
        hit_set = set(hits)
        for r, i in hits:
            lo, hi = max(0, i - context), min(len(msgs[r]) - 1, i + context)
            same = [k for k, b in enumerate(blocks) if b[0] == r and lo <= b[2] + 1 and hi >= b[1] - 1]
            if same:
                k = same[-1]
                blocks[k] = (r, min(lo, blocks[k][1]), max(hi, blocks[k][2]))
            else:
                blocks.append((r, lo, hi))
        blocks.sort(key=lambda b: msgs[b[0]][b[1]].ts)
        parts = ["\n".join(line(msgs[r][i], "* " if (r, i) in hit_set else "  ") for i in range(lo, hi + 1))
                 for r, lo, hi in blocks]
        head += f"；带 * 的是命中的消息，其余是前后各 {context} 条上下文，段与段之间用 …… 隔开"
        return note + head + "。\n" + "\n……\n".join(parts)

    def files(self, group: str = "", days: float = 30, query: str = "") -> str:
        start = datetime.now() - timedelta(days=days)
        note, me, names, rooms, msgs = self._collect(group, start, datetime.now() + timedelta(days=1))
        lines = []
        for m in sorted((m for ms in msgs.values() for m in ms), key=lambda m: m.ts):
            for title, size, ts in m.files or []:
                if query and query.lower() not in title.lower():
                    continue
                who = "我" if m.sender == me else names.of(m.sender, m.room)
                tag = f"[{names.of(m.room)}] " if len(rooms) > 1 else ""
                lines.append(f"{datetime.fromtimestamp(m.ts):%m-%d %H:%M} {tag}{who}："
                             + self._files.describe(title, size, ts))
        scope = names.of(rooms[0]) if len(rooms) == 1 else f"{len(rooms)} 个群"
        head = f"{scope}最近 {days:g} 天" + (f"名字含“{query}”的" if query else "") + f"文件 {len(lines)} 个，旧的在前。"
        return note + head + "\n" + "\n".join(lines)
