# HTTP transport (streamable HTTP, MCP 2025-06-18)

同一个 MCP server 也可以跑成常驻 HTTP 端点：多个客户端共用一个进程，搜索缓存、
引擎冷却、密钥只在一处生效。协议按 MCP **streamable HTTP** 实现，响应是最普通的
`application/json`（没有 SSE 流，服务端不主动推送消息）。

## 启动

```bash
agg-search-http --host 127.0.0.1 --port 8765 --path /mcp
# 等价：python3 -m agg_search.http_transport ...
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址；非回环地址会打印"未认证端点"警告 |
| `--port` | `8765` | 端口 |
| `--path` | `/mcp` | 端点路径，其他路径一律 404 |
| `--allow-origin` | 空 | 额外允许的 `Origin`（可重复），默认只允许回环 |
| `--allow-host` | 空 | 额外允许的 `Host` 主机名（可重复），防 DNS rebinding |

## 客户端配置

```bash
# Claude Code
claude mcp add --transport http --scope user agg-search http://127.0.0.1:8765/mcp
# Codex
codex mcp add agg-search --url http://127.0.0.1:8765/mcp
```

Cursor / VS Code / OpenCode 等"URL 型"配置：

```json
{
  "mcpServers": {
    "agg-search": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

VS Code 的 `.vscode/mcp.json` 用 `servers` 键，`type` 写 `http`，其余相同。

## 协议行为

- `POST`：单条 JSON-RPC → `200 application/json`；通知 → `202` 空响应；
  解析失败 → `-32700`；未知方法 `-32601`；未知工具 `-32602`。
- 批量数组：返回响应数组（通知不产生条目；空数组 → `-32600`）。
- `initialize` 下发 `Mcp-Session-Id`；之后携带未知 id 的请求直接 `404`。
  会话表是 1000 条的有界 FIFO。
- `GET`：不带 `Accept: text/event-stream` → `405`；带则开一个立即关闭的 SSE 流。
- `DELETE` → `405`。
- 请求体上限 1 MiB（超过 `413`）；非 JSON `Content-Type` → `415`。

## 安全模型

**这个端点没有认证。** 默认只绑回环，并拒绝 `Origin` 不是回环的请求（挡浏览器
发起的跨站请求），以及 `Host` 保险丝（挡 DNS rebinding）。需要跨机器访问时：

1. 用 `--host 0.0.0.0` 绑定并接受警告；
2. 前面必须放一层带认证/ TLS 的反向代理（nginx/Caddy），并让代理传
   `Host`、`Origin`；同时用 `--allow-host`/`--allow-origin` 收紧白名单。

```nginx
location /mcp {
    auth_basic "mcp";
    auth_basic_user_file /etc/nginx/mcp.htpasswd;
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_read_timeout 600s;   # 工具调用时长上限 120s/300s
}
```

工具调用跑在各自的处理线程里，`mcp.call_tool` 自带 120s（search）/ 300s（fetch）
墙钟，慢引擎不会卡住 accept 循环。每个请求在 stderr 打一行
`method path -> status (N ms)`，不记录请求体。
