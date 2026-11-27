# wechat-mcp

让 Claude Code 这类 MCP 客户端只读地查看指定的微信群。注册为用户级 MCP server 后，所有会话都能用。它只读得到 `config.toml` 白名单里的群，私聊和其他群一概读不到。之所以这样设计，是因为用户级 MCP 对每个会话都开放，包括那些正在处理你不完全信任的代码的会话。

只支持 Windows 版微信 4.x，在 4.2.0.53 上测试过。

## 工具

- `wechat_groups`：列出可读的群，以及每个群最后一条消息的时间和最近 7 天的消息数。
- `wechat_messages`：按群、时间段、正文关键词、发送者读消息，也可以只看 @我 和 @所有人 的消息。设了过滤条件时，每条命中的消息会带上前后几条作为上下文；不指定时间时默认搜全部历史。时间可以写 `2026-11-20 14:00`，也可以写 `昨天`、`本周`、`3h`、`2d`。
- `wechat_files`：列出群里发过的文件，包括合并转发里夹带的文件。

消息显示规则：合并转发的聊天记录会逐条展开，嵌套的也会展开；引用回复带上被引用的原文；群文件如果已经被微信下载到本机，会附上本地路径，会话可以直接打开读内容，没下载的会注明；图片和语音只显示类型。发送者名字优先用你给的备注，其次是群昵称，最后是微信昵称。

## 原理

微信 4.x 的本地库用 SQLCipher 4 加密。每次调用时，`wechat_mcp/decrypt.py` 会检查 `contact.db` 和 `message_*.db` 有没有变化，把有变化的重新解密，连同还没合并进主文件的 WAL 一起写到 `data/<微信号>/`，耗时一两秒；距上次检查不到 30 秒的调用直接读现有副本，所以总能读到最新消息。每个会话各自启动一个 server 进程，这些进程共用 `data/`，解密和读取都在 `data/.lock` 这把文件锁里进行。

本项目不负责获取密钥。密钥读取自 [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis) 保存的 `account_keys.json`，所以要先用它取过一次密钥。密钥只在本机进程内使用，不会出现在任何返回内容里。

## 安装

```powershell
git clone https://github.com/assass1nlike/wechat-mcp
cd wechat-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -e .
copy config.example.toml config.toml
.venv\Scripts\wechat-mcp groups        # 列出本机所有群和 id，把要开放的群填进 config.toml
claude mcp add -s user wechat -- <项目绝对路径>/.venv/Scripts/wechat-mcp.exe
```

修改 `config.toml` 之后不用重启，下次调用就会生效。

换电脑时，代码和 `config.toml` 可以直接带走，但密钥要在新电脑上用 WeChatDataAnalysis 重新取一次。新电脑的微信默认只有登录之后收到的消息，要看以前的记录，得先用微信自带的聊天记录迁移功能把记录迁过去。

## 密钥失效

重新登录微信后，密钥可能会变。这时工具会提示密钥对不上，改为读取上一次解出的副本，并注明记录截止到什么时候。用 WeChatDataAnalysis 重新取一次密钥就能恢复。

## 声明

只用于读取你自己的微信数据。解密本地数据库可能违反微信的用户协议，后果自行承担。
