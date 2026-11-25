# wechat-mcp

让 Claude Code 只读地查看指定的微信群消息。注册为用户级 MCP server 后，所有会话都能用。只能查 `config.toml` 白名单里的群，其他聊天一概读不到。

## 工具

- `wechat_groups`：列出可读的群、每个群最后一条消息的时间和最近 7 天的消息数。
- `wechat_messages`：读消息，按时间从旧到新排列。`group` 填群名的一部分（留空则读所有可读的群），时间范围用 `days`，或者用 `since`/`until`（`YYYY-MM-DD [HH:MM]`）；`query` 按正文关键词过滤，`sender` 按发送者过滤，`limit` 限制条数（默认 300，超出时保留最新的）。

引用回复会带上被引用的内容，文件、链接、聊天记录、接龙显示标题和摘要，图片和语音只显示类型。发送者的显示顺序依次是：备注、群昵称、微信昵称。

## 原理

微信 4.x 的本地库是 SQLCipher 4 加密的。每次调用时，`wechat_mcp/decrypt.py` 会把有变化的 `contact.db` 和 `message_*.db` 重新解密，连同还没合并进主文件的 WAL，写到 `data/<微信号>/`，耗时一两秒；距离上次检查不到 30 秒的调用直接读现有副本。密钥取自 WeChatDataAnalysis 保存的 `account_keys.json`，所以要先用它取过一次密钥。密钥只在本机进程里使用，不会出现在返回内容里。解密代码来自 AnotherMe 的 `workbench/wechat_db.py`。

每个 Claude Code 会话都会各自启动一个 server 进程，它们共用 `data/`，解密和读取都放在 `data/.lock` 这把文件锁里进行。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
copy config.example.toml config.toml   # 然后填群 id
claude mcp add -s user wechat -- D:/lxxzyh/wechat-mcp/.venv/Scripts/wechat-mcp.exe
```

群 id 的形式是 `数字@chatroom`，可以在 AnotherMe 的 `wechat_chats` 里查到，也可以去 `data/<微信号>/contact.db` 的 `contact` 表里按群名查。改 `config.toml` 之后不用重启，下次调用就会生效。

## 密钥失效

如果重新登录微信后密钥变了，工具会返回"微信密钥对不上"，并改为读取上一次解出的副本，同时注明记录截止到什么时候。这时用 WeChatDataAnalysis 重新取一次密钥即可。
