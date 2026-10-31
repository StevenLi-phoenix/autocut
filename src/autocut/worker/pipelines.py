"""流水线注册表（cut / commentary / montage / highlights 逐阶段加入）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autocut.worker.server import App


def register(app: App) -> None:
    """把各流水线挂到 app.pipelines。"""
    from autocut.worker import pipe_cut

    from autocut.worker import pipe_commentary

    from autocut.worker import pipe_clips

    from autocut.worker import pipe_montage

    app.pipelines["montage"] = pipe_montage.pipeline_montage
    app.pipelines["clips"] = pipe_clips.pipeline_clips
    app.pipelines["cut"] = pipe_cut.pipeline_cut
    app.pipelines["commentary"] = pipe_commentary.pipeline_commentary
