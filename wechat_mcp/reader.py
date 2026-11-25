"""只读访问白名单里的微信群。

库的结构：联系人在 contact.db 的 contact（好友）和 stranger（非好友）表，群昵称在 chat_room.ext_buffer（protobuf）；
消息在 message_0.db、message_1.db…… 里按会话分表，表名是 Msg_ 加群 id 的 md5，较长的内容用 zstd 压缩。
"""

import hashlib
import html
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import zstandard
from filelock import FileLock

from . import decrypt
from .config import DATA

KINDS = {3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情", 48: "位置", 50: "通话"}
APP_KINDS = {5: "链接", 6: "文件", 19: "聊天记录", 33: "小程序", 36: "小程序", 51: "视频号", 53: "接龙", 2000: "转账"}
_zstd = zstandard.ZstdDecompressor()
_lock = FileLock(str(DATA / ".lock"), timeout=120)


class Unavailable(Exception):
    pass


def _open(d: Path, name: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{(d / name).as_posix()}?mode=ro&immutable=1", uri=True)


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


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    out = shift = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, i
        shift += 7


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


def _xml(text: str):
    try:
        return ET.fromstring(text[text.index("<"):])
    except (ValueError, ET.ParseError):
        return None


def _text(kind: int, compressed, content) -> str:
    """把一条消息变成一行可读的文字（发送者前缀已去掉）。"""
    if isinstance(content, bytes):
        if compressed == 4:
            try:
                content = _zstd.decompress(content)
            except zstandard.ZstdError:
                return "[无法解压的内容]"
        content = content.decode("utf-8", "replace")
    content = re.sub(r"^[A-Za-z0-9_@.-]+:\n", "", content or "")  # 群消息正文前的发送者 id
    kind &= 0xFFFFFFFF
    if kind == 1:
        return content
    if kind == 49:
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
            return f"{title}（回复 {who}：{quoted[:80]}）"
        label = APP_KINDS.get(sub, "分享")
        des = re.sub(r"\s+", " ", (app.findtext("des") or "").strip())
        if sub in (19, 53) and des:
            return f"[{label}] {title}：{des[:300]}"
        url = (app.findtext("url") or "").strip() if sub == 5 else ""
        return f"[{label}] {title}" + (f" {url}" if url else "")
    if kind == 10000:  # 系统提示：撤回、进群、改群名等
        root = _xml(content)
        if root is None:
            return f"[系统] {re.sub(r'<[^>]+>', '', content).strip()}"
        if root.get("type") == "revokemsg":
            return f"[系统] {(root.findtext('.//content') or '有人撤回了一条消息').strip()}"
        if root.get("type") == "mmchatroomtopmsg":
            return f"[系统] {root.findtext('.//nickname') or '有人'} 修改了群置顶消息"
        tmpl = root.findtext(".//template") or ""
        for link in root.iter("link"):
            names = [m.findtext("nickname") or "" for m in link.iter("member")]
            value = "、".join(n for n in names if n) or (link.findtext("plain") or "")
            tmpl = tmpl.replace(f"${link.get('name')}$", value)
        return f"[系统] {tmpl.strip() or '（无法解析）'}"
    return f"[{KINDS.get(kind, f'类型 {kind}')}]"


class Reader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        groups = cfg.get("groups", [])
        self.rooms: list[str] = [g["id"] for g in groups]
        if not self.rooms:
            raise Unavailable("config.toml 里没有配置任何群")

    def _prepare(self) -> tuple[Path, str, str, Names]:
        d, me, note = decrypt.refresh(self.cfg)
        return d, me, note, Names(d, self.rooms)

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
    def _rows(d: Path, room: str, since: float, until: float):
        table = "Msg_" + hashlib.md5(room.encode()).hexdigest()
        out = []  # 一个群的消息可能分散在几个 message 库里，发送者编号是每个库各自的
        for f in sorted(d.glob("message_[0-9]*.db")):
            con = _open(d, f.name)
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                senders = dict(con.execute("SELECT rowid, user_name FROM Name2Id"))
                found = con.execute(
                    f'SELECT create_time, real_sender_id, local_type, WCDB_CT_message_content, message_content '
                    f'FROM "{table}" WHERE create_time >= ? AND create_time < ?', (since, until))
                out += [(ts, senders.get(s, ""), k, c, m) for ts, s, k, c, m in found]
            con.close()
        return out

    def groups(self) -> str:
        with _lock:
            d, me, note, names = self._prepare()
            week = (datetime.now() - timedelta(days=7)).timestamp()
            lines = []
            for r in self.rooms:
                rows = self._rows(d, r, 0, 1e12)
                last = max((x[0] for x in rows), default=0)
                recent = sum(1 for x in rows if x[0] >= week)
                when = f"最后一条 {datetime.fromtimestamp(last):%Y-%m-%d %H:%M}" if last else "没有消息"
                lines.append(f"{names.of(r)}（{r}）：{when}，最近 7 天 {recent} 条，共 {len(rows)} 条")
        return note + "\n".join(lines)

    def messages(self, group: str = "", days: float = 3, since: str = "", until: str = "",
                 query: str = "", sender: str = "", limit: int = 300) -> str:
        with _lock:
            d, me, note, names = self._prepare()
            rooms = self._match(names, group)
            start = _parse_time(since) if since else datetime.now() - timedelta(days=days)
            end = _parse_time(until) if until else datetime.now() + timedelta(days=1)
            rows = []
            for r in rooms:
                rows += [(ts, r, who, k, c, m) for ts, who, k, c, m in
                         self._rows(d, r, start.timestamp(), end.timestamp())]
        rows.sort(key=lambda x: x[0])
        lines = []
        for ts, room, who, kind, compressed, content in rows:
            text = _text(kind, compressed, content)
            label = "我" if who == me else ("系统" if not who or who == room else names.of(who, room))
            if query and query.lower() not in text.lower():
                continue
            if sender and sender.lower() not in label.lower():
                continue
            tag = f"[{names.of(room)}] " if len(rooms) > 1 else ""
            lines.append(f"{datetime.fromtimestamp(ts):%m-%d %H:%M} {tag}{label}：{text[:1000]}")
        cut = len(lines) > limit
        lines = lines[-limit:]
        scope = names.of(rooms[0]) if len(rooms) == 1 else f"{len(rooms)} 个群"
        cond = "".join([f"含“{query}”" if query else "", f"发送者含“{sender}”" if sender else ""])
        head = f"{scope}，{start:%Y-%m-%d %H:%M} 起{('、' + cond) if cond else ''}的消息 {len(lines)} 条，旧的在前"
        if cut:
            head += f"（超过 {limit} 条，只保留最新的；要看更早的用 until 往前翻）"
        return note + head + "。\n" + "\n".join(lines)


def _parse_time(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            t = datetime.strptime(s, fmt)
            return t.replace(year=datetime.now().year) if "%Y" not in fmt else t
        except ValueError:
            continue
    raise Unavailable(f"看不懂时间“{s}”，请用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
