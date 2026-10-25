# 回归测试导航

先按主说明安装运行依赖，在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
```

| 测试文件 | 验证内容 |
| --- | --- |
| [test_chat.py](test_chat.py) | 验证 Mini 会话隔离、消息流、续聊与权限决定 |
| [test_codex.py](test_codex.py) | 验证 Codex 事件解析、状态转换与增量读取 |
| [test_codex_stats.py](test_codex_stats.py) | 验证历史统计的去重、日期范围和缓存恢复 |

这些测试使用临时记录与本地传输替身，不会向 Claude / Codex 发起模型请求。GUI 运行依赖仍须安装，因为会话与聊天模块使用 Qt 信号。
