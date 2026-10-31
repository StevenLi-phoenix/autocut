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

    app.pipelines["montage_prepare"] = pipe_montage.pipeline_prepare
    app.pipelines["montage_finish"] = pipe_montage.pipeline_finish
    app.pipelines["clips_deliver"] = pipe_clips.pipeline_deliver
    from autocut.worker import pipe_edl

    app.pipelines["edl_finish"] = pipe_edl.pipeline_finish
    app.pipelines["cut_prepare"] = pipe_cut.pipeline_prepare
    app.pipelines["cut_finish"] = pipe_cut.pipeline_finish
    app.pipelines["commentary_prepare"] = pipe_commentary.pipeline_prepare
    app.pipelines["commentary_finish"] = pipe_commentary.pipeline_finish
