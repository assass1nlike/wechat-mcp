import sys

from mcp.server.mcpserver import MCPServer

from . import config, decrypt, reader

server = MCPServer(
    "wechat",
    instructions="只读查看用户的科研微信群消息（只能看 config.toml 白名单里的群）。"
                 "先用 wechat_groups 看有哪些群，再用 wechat_messages 读消息或搜索，wechat_files 找群里发过的文件。",
)


def _reader() -> reader.Reader:
    return reader.Reader(config.load())  # 每次调用都重读配置，改白名单不用重启


def _safe(fn):
    try:
        return fn()
    except (reader.Unavailable, decrypt.Error, FileNotFoundError) as e:
        return str(e)


@server.tool()
def wechat_groups() -> str:
    """列出可读的科研微信群，以及每个群最后一条消息的时间和最近 7 天的消息数。"""
    return _safe(lambda: _reader().groups())


@server.tool()
def wechat_messages(group: str = "", days: float | None = None, since: str = "", until: str = "",
                    query: str = "", sender: str = "", mentions: bool = False, context: int = 2,
                    limit: int = 300) -> str:
    """读或搜索科研微信群的消息，按时间从旧到新排列。

    合并转发的聊天记录会逐条展开；群文件会附上本机路径（微信已下载时），可以直接打开读内容。

    group: 群名的一部分或群 id；留空则读所有可读的群。
    days: 看最近几天。不填时，浏览默认 3 天；给了 query/sender/mentions 时默认搜全部历史。
    since / until: 起止时间。可写 2026-11-20、2026-11-20 14:00、今天、昨天、前天、本周、上周（表示那天 0 点），
        或 30m、3h、2d、1w（从现在往前推）。给了 since 时忽略 days。
    query: 只看正文（含展开的聊天记录、文件名）包含这个词的消息，不区分大小写。
    sender: 只看发送者名字包含这个词的消息。
    mentions: 只看 @我 或 @所有人 的消息。
    context: 有过滤条件时，每条命中的消息前后各带几条上下文；0 表示不带。
    limit: 最多返回多少条（有过滤条件时指命中的条数），超出时保留最新的。
    """
    return _safe(lambda: _reader().messages(group, days, since, until, query, sender, mentions, context, limit))


@server.tool()
def wechat_files(group: str = "", days: float = 30, query: str = "") -> str:
    """列出科研微信群里发过的文件（包括合并转发里的），附本机路径；没下载的会注明。

    group: 群名的一部分或群 id，留空则看所有可读的群。
    days: 看最近几天，默认 30。
    query: 只看文件名包含这个词的。
    """
    return _safe(lambda: _reader().files(group, days, query))


def _list_groups():
    """在终端里列出本机微信的所有群和 id，配置白名单时用。不经过 MCP，不受白名单限制。"""
    cfg = config.load()
    with reader._lock:
        d, _, note, _ = decrypt.refresh(cfg)
        groups = reader.all_groups(d)
    allowed = {g["id"] for g in cfg.get("groups", [])}
    print(note, end="")
    for name, gid in groups:
        print(f"{'*' if gid in allowed else ' '} {gid}  {name}")
    print(f"\n共 {len(groups)} 个群，* 是已在白名单里的")


def main():
    if sys.argv[1:] == ["groups"]:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        _list_groups()
    elif sys.argv[1:]:
        print("用法：wechat-mcp           启动 MCP server（由 Claude Code 调用）\n"
              "      wechat-mcp groups    列出本机微信的所有群和 id")
    else:
        server.run("stdio")
