"""实时字幕延迟与改写复盘系统。

模块划分：
  textnorm  分词与规范化（CJK 单字 / 拉丁词 / 标点）
  alignment 终稿↔参考稿对齐、快照↔终稿对齐
  anchors   日志时钟 → 音频时钟的锚点拟合
  pipeline  快照追踪、指标计算、受影响区间重算
  layout    断行与滚屏复核：窗口布局、呈现重建、可读性检查
  store     SQLite 持久化
  exports   WebVTT / CSV / SVG / JSON 导出
"""

__version__ = "1.1.0"
