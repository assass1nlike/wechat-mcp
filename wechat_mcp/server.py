from mcp.server.mcpserver import MCPServer

from . import config, decrypt, reader

server = MCPServer(
    "wechat",
    instructions="只读查看用户的科研微信群消息（只能看 config.toml 白名单里的群）。"
                 "先用 wechat_groups 看有哪些群，再用 wechat_messages 读消息或按关键词查。",
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
def wechat_messages(group: str = "", days: float = 3, since: str = "", until: str = "",
                    query: str = "", sender: str = "", limit: int = 300) -> str:
    """读科研微信群的消息，按时间从旧到新排列。

    group: 群名的一部分或群 id；留空则读所有可读的群并合在一起。
    days: 读最近几天，默认 3；给了 since 时忽略。
    since / until: 起止时间，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。
    query: 只留正文包含这个词的消息（不区分大小写）。
    sender: 只留发送者名字包含这个词的消息。
    limit: 最多返回多少条，超出时保留最新的。
    """
    return _safe(lambda: _reader().messages(group, days, since, until, query, sender, limit))


def main():
    server.run("stdio")
