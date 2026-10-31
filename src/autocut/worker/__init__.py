"""autocut-worker：把 autocut CLI 包成本机 HTTP 服务，供 DSH agent 调用。

边界（报告 P0-2）：子命令白名单、路径限定在 `<root>/<job>/`、异步任务 + 进度、按 PID 取消、重活互斥。
autocut 里没有任何 LLM 调用：判断（剪哪里、写解说稿、选段、挑镜头）全部由 DSH agent 做，worker 只做确定性的手和眼。
"""
