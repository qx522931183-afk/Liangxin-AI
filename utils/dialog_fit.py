"""
dialog_fit.py — 让弹窗「装得下内容、又不超出屏幕」

为什么需要这个工具
──────────────────
QDialog 默认的 sizeConstraint 是 SetDefaultConstraint，它**不会**把窗口
撑到内容真正需要的尺寸。如果代码里写死 `resize(460, 600)`，而对话框内容
实际需要 732 高，Qt 就会去压缩子控件来迁就窗口，表现为：

  · 文字被上下裁掉（"思考延迟" 只剩一半）
  · 滑块被压得很短
  · 输入框、下拉框重叠或错位
  · 底部按钮看不到

调大窗口就正常了 —— 但用户每次都要手动拉，这就是「看不全」的根源。

解决办法：在对话框构建完 UI 之后调用 fit_dialog()，让它根据**布局自己的
最小尺寸需求**来定窗口大小，同时用屏幕可用区域封顶，避免窗口跑到屏幕外面。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QApplication, QDialog, QWidget

# 窗口最多占屏幕可用区域的比例（留出任务栏和边框余量）
_MAX_W_RATIO = 0.92
_MAX_H_RATIO = 0.88


def content_need(widget: QWidget) -> tuple[int, int]:
    """返回 widget 内容真正需要的最小尺寸 (宽, 高)。

    优先用 layout.minimumSize()（子控件压缩到这个尺寸就会变形），
    拿不到时退回 sizeHint()。
    """
    layout = widget.layout()
    if layout is not None:
        need = layout.minimumSize()
        if need.width() > 0 and need.height() > 0:
            return need.width(), need.height()
        hint = layout.sizeHint()
        if hint.width() > 0 and hint.height() > 0:
            return hint.width(), hint.height()

    hint = widget.sizeHint()
    return max(hint.width(), 1), max(hint.height(), 1)


def screen_limit(margin_x: int = 0, margin_y: int = 0) -> tuple[int, int]:
    """当前主屏可用区域的尺寸（已按比例留边）。"""
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1280, 720
    avail = screen.availableGeometry()
    return (
        max(320, int(avail.width() * _MAX_W_RATIO) - margin_x),
        max(240, int(avail.height() * _MAX_H_RATIO) - margin_y),
    )


def fit_dialog(dialog: QWidget,
               design_width: int | None = None,
               design_height: int | None = None,
               min_width: int = 0,
               min_height: int = 0,
               margin_x: int = 0,
               margin_y: int = 0) -> tuple[int, int]:
    """把对话框尺寸调整到「内容够用 + 屏幕装得下」，返回最终 (宽, 高)。

    参数
    ----
    design_width/height : 设计者期望的尺寸（只是下限参考，内容需要更大时会被抬高）
    min_width/height    : 硬性下限（比内容需求还小的场景，比如空内容的设置页）
    margin_x/y          : 额外要预留的空间（比如给标题栏留高度）

    行为
    ----
    1. 取 max(设计尺寸, 内容需求, 硬性下限) 作为期望尺寸
    2. 用屏幕可用区域封顶
    3. 把封顶后的尺寸同时设为 minimumSize 和当前尺寸 ——
       这样用户也没法把它拖到挤压内容的程度
    """
    need_w, need_h = content_need(dialog)
    limit_w, limit_h = screen_limit(margin_x, margin_y)

    want_w = max(design_width or 0, need_w, min_width)
    want_h = max(design_height or 0, need_h, min_height)

    final_w = min(want_w, limit_w)
    final_h = min(want_h, limit_h)

    dialog.setMinimumSize(final_w, final_h)
    dialog.resize(final_w, final_h)
    return final_w, final_h


def attach_auto_fit(dialog: QWidget, *args, **kwargs):
    """延迟一帧再 fit。

    有些对话框在 __init__ 里就把控件加好了，但样式表、字体、动态内容
    （比如从配置读出来的标签）要到事件循环跑起来才定型。用 singleShot(0)
    等布局稳定后再算尺寸，结果更准。
    """
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(0, lambda: fit_dialog(dialog, *args, **kwargs))


def enable_auto_fit(dialog: QWidget):
    """在对话框首次显示时自动把尺寸扩到「内容装得下」。

    特点：**只增不减**。它把对话框当前的宽高当作设计下限，
    只有内容需求更大时才撑大，因此对已经调好的窗口完全无副作用，
    可以放心给一批对话框统一加上作为兜底。

    适用场景：对话框需要构造参数、没法在 __init__ 末尾统一处理，
    或者内容长度随数据变化（列表、统计、图表）时。
    """
    original_show_event = type(dialog).showEvent

    def _show_event(self, event):
        if not getattr(self, "_auto_fit_applied", False):
            self._auto_fit_applied = True
            try:
                fit_dialog(self, design_width=self.width(), design_height=self.height())
            except Exception:
                pass
        original_show_event(self, event)

    # 绑定到实例，保留类上原有的 showEvent 行为
    dialog.showEvent = _show_event.__get__(dialog, type(dialog))
    return dialog
