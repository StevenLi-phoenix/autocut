"""autocut-worker：把 autocut CLI 包成本机 HTTP 服务，供 DSH agent 调用。

边界（报告 P0-2）：子命令白名单、路径限定在 `<root>/<job>/`、异步任务 + 进度、按 PID 取消、重活互斥。
worker 不含 LLM；LLM 判断在 pipelines（走 llm-gateway，带预算）或 DSH agent 里。
"""
