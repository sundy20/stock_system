"""
动态调权模块 v4.4

基于历史信号归因数据，自动调整信号权重以优化回测收益。

功能：
  - load_signal_attribution: 从CSV加载归因数据
  - calculate_optimal_weights: 基于累积收益计算最优权重
  - apply_signal_weights: 应用权重到回测逻辑

使用方法：
  python3 app/dynamic_weights.py                   # 查看权重报告
  python3 app/dynamic_weights.py --apply           # 应用优化权重到 config.yaml
"""

import os
import csv
import logging
from typing import Dict

logger = logging.getLogger("dynamic_weight")


class SignalAttribution:
    """信号归因数据结构"""

    def __init__(self, signal_type: str, trade_count: int, avg_return: float,
                 win_rate: float, cumulative_return: float):
        self.signal_type = signal_type
        self.trade_count = trade_count
        self.avg_return = avg_return
        self.win_rate = win_rate
        self.cumulative_return = cumulative_return

    @property
    def is_valid(self) -> bool:
        """是否为有效信号（非亏损且胜率合理）"""
        return self.cumulative_return > 0 and self.trade_count >= 10

    @property
    def quality_score(self) -> float:
        """
        质量分数 = 权重 * 胜率 * 收益率
        用于评估信号的真实质量
        """
        return self.trade_count * self.win_rate / 100 * self.avg_return


class DynamicWeightOptimizer:
    """动态权重优化器"""

    # 默认权重（无归因数据时使用）
    DEFAULT_WEIGHTS = {
        '全信号共振': 5.0,
        '弹性降级：月回踩+月布林+周回踩+周布林': 12.0,
        '弹性降级：月回踩+月布林+周回踩': 15.0,
        '弹性降级：月回踩+月布林+周布林': 8.0,
        '弹性降级：月回踩+周回踩+周布林': 6.0,
        '弹性降级：月布林+周回踩+周布林': 8.0,
        '弹性降级：月回踩+月布林': 10.0,
        '弹性降级：月回踩+周回踩': 7.0,
        '弹性降级：月回踩+周布林': 4.0,
        '弹性降级：月布林+周回踩': 8.0,
        '弹性降级：周回踩+周布林': 3.0,
    }

    # 归因信号类型 → config.yaml 标准 key（与 backtest_runner._build_signal_scores 一致）
    ATTR_TO_CONFIG_KEY = {
        '全信号共振': 'all_resonance',
        '弹性降级：月回踩+月布林+周回踩+周布林': 'mr_mb_wr_wb',
        '弹性降级：月回踩+月布林+周回踩': 'mr_mb_wr',
        '弹性降级：月回踩+月布林+周布林': 'mr_mb_wb',
        '弹性降级：月回踩+周回踩+周布林': 'mr_wr_wb',
        '弹性降级：月布林+周回踩+周布林': 'mb_wr_wb',
        '弹性降级：月回踩+月布林': 'mr_mb',
        '弹性降级：月回踩+周回踩': 'mr_wr',
        '弹性降级：月回踩+周布林': 'mr_wb',
        '弹性降级：月布林+周回踩': 'mb_wr',
        '弹性降级：周回踩+周布林': 'wr_wb',
    }

    def __init__(self, attribution_file: str = 'signal_attribution.csv'):
        self.attribution_file = attribution_file
        self.attribution_data: Dict[str, SignalAttribution] = {}

    def load_signal_attribution(self, file_path: str = None) -> Dict[str, SignalAttribution]:
        """
        加载信号归因数据

        返回: {信号类型: SignalAttribution对象}
        """
        path = file_path or self.attribution_file
        if not os.path.exists(path):
            logger.warning(f"归因文件不存在: {path}，使用默认权重")
            return {}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    signal_type = row['信号类型']
                    trade_count = int(row['交易次数'])
                    avg_return = float(row['平均收益率(%)'])
                    win_rate = float(row['胜率(%)'])
                    cumulative_return = float(row['累积收益(%)'])

                    self.attribution_data[signal_type] = SignalAttribution(
                        signal_type=signal_type,
                        trade_count=trade_count,
                        avg_return=avg_return,
                        win_rate=win_rate,
                        cumulative_return=cumulative_return
                    )

            logger.info(f"加载归因数据: {len(self.attribution_data)} 个信号")
            return self.attribution_data

        except Exception as e:
            logger.error(f"加载归因文件失败: {e}")
            return {}

    def _attr_to_config_key(self, signal_type: str) -> str:
        """将归因信号类型映射为 config.yaml 的标准 key"""
        if signal_type in self.ATTR_TO_CONFIG_KEY:
            return self.ATTR_TO_CONFIG_KEY[signal_type]

        # 模糊匹配
        for attr_type, config_key in self.ATTR_TO_CONFIG_KEY.items():
            if signal_type in attr_type or attr_type in signal_type:
                return config_key
        return signal_type

    def calculate_optimal_weights(self, use_cumulative: bool = True) -> Dict[str, float]:
        """
        基于归因数据计算最优权重。

        公式：score = cumulative_return × trade_count（use_cumulative=True）
              weight = min(score / total_score × 100, 50)，上限50防止归一化异常

        亏损信号（cumulative_return ≤ 0 或 trade_count < 10）直接给 0 分。
        无归因数据时回退到 DEFAULT_WEIGHTS。
        """
        attribution = self.load_signal_attribution()

        if not attribution:
            logger.info("无归因数据，使用默认权重")
            return self.DEFAULT_WEIGHTS.copy()

        # 计算每个信号的基础分
        scores = []
        for real_type, attr in attribution.items():
            if attr.is_valid:
                if use_cumulative:
                    score = attr.cumulative_return * attr.trade_count
                else:
                    score = attr.avg_return * attr.trade_count
                scores.append((real_type, score))
            else:
                # 亏损信号或交易次数太少，权重为0
                scores.append((real_type, 0.0))

        # 按分数排序
        scores.sort(key=lambda x: x[1], reverse=True)

        # 分配权重
        weights = {}
        total_score = sum(s[1] for s in scores)

        if total_score > 0:
            for signal_type, score in scores:
                if score > 0:
                    # 对数缩放：避免单一信号权重爆表
                    # weight = min(ln(score) * 2, 50)
                    weight = min(round(score / total_score * 100, 1), 50.0)
                else:
                    weight = 0.0
                weights[signal_type] = weight
        else:
            # 所有效益都为0，使用默认权重
            weights = self.DEFAULT_WEIGHTS.copy()

        # 确保至少有一个信号的权重不为0
        max_weight = max(weights.values()) if weights else 0
        if max_weight == 0:
            weights = self.DEFAULT_WEIGHTS.copy()

        logger.info("计算最优权重:")
        for signal_type in sorted(weights.keys()):
            logger.info("  %s: %.1f", signal_type, weights[signal_type])

        return weights

    def apply_to_config(self, config_path: str = 'config.yaml',
                       weights: Dict[str, float] = None) -> Dict[str, float]:
        """
        将权重写入 config.yaml 的 strategy.signal_ranking。
        文本级替换 signal_ranking 块，保留文件中所有注释和其他配置。
        """
        if weights is None:
            weights = self.calculate_optimal_weights()

        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在: {config_path}")
            return {}

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # 找到 signal_ranking: 行
            sr_line_idx = None
            sr_indent = 0
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith('signal_ranking:'):
                    sr_line_idx = i
                    sr_indent = len(line) - len(line.lstrip())
                    break

            if sr_line_idx is None:
                logger.error("配置文件中未找到 signal_ranking 字段")
                return {}

            # 删除 signal_ranking 下所有缩进更深的子行
            # 删除规则：缩进 > sr_indent 的行全部删掉（包括子注释）
            # 遇到缩进 <= sr_indent 且非空的行时停止
            end_idx = sr_line_idx + 1
            while end_idx < len(lines):
                line = lines[end_idx]
                stripped = line.strip()
                if stripped == '':
                    # 空行：查看下一行决定是否保留
                    # 如果下一行缩进 > sr_indent，则删除当前空行（子块内部空行）
                    # 否则保留（区块分隔空行）
                    if end_idx + 1 < len(lines):
                        next_indent = len(lines[end_idx + 1]) - len(lines[end_idx + 1].lstrip())
                        if next_indent > sr_indent:
                            end_idx += 1
                            continue
                    break
                indent = len(line) - len(stripped)
                if indent > sr_indent:
                    end_idx += 1
                    continue
                break

            # 删除旧的子行
            del lines[sr_line_idx + 1:end_idx]

            # 构建新的 signal_ranking 键值对
            sr = {}
            for attr_type, weight in weights.items():
                config_key = self._attr_to_config_key(attr_type)
                sr[config_key] = weight

            child_indent = sr_indent + 2
            prefix = ' ' * child_indent
            new_lines = []
            for key, val in sorted(sr.items(), key=lambda x: -x[1]):
                new_lines.append(f"{prefix}{key}: {val}\n")

            # 插入到 signal_ranking: 行之后
            for i, new_line in enumerate(new_lines):
                lines.insert(sr_line_idx + 1 + i, new_line)

            with open(config_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            logger.info(f"已更新配置文件: {config_path}（注释保留）")
            return sr

        except Exception as e:
            logger.error(f"更新配置文件失败: {e}")
            return {}

    def export_weight_report(self, output_file: str = 'weight_report.txt'):
        """
        导出权重优化报告

        包含：
          - 当前权重
          - 信号质量评分
          - 建议权重
        """
        attribution = self.load_signal_attribution()

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write("信号权重优化报告\n")
            f.write("=" * 60 + "\n\n")

            if not attribution:
                f.write("无归因数据，使用默认权重\n\n")
                f.write("默认权重:\n")
                for k, v in self.DEFAULT_WEIGHTS.items():
                    f.write(f"  {k}: {v}\n")
                return

            f.write("信号质量统计:\n")
            f.write("-" * 60 + "\n")
            for signal_type, attr in sorted(attribution.items(),
                                           key=lambda x: x[1].cumulative_return,
                                           reverse=True):
                f.write(f"{signal_type}:\n")
                f.write(f"  交易次数: {attr.trade_count}\n")
                f.write(f"  平均收益: {attr.avg_return:.2f}%\n")
                f.write(f"  胜率: {attr.win_rate:.1f}%\n")
                f.write(f"  累积收益: {attr.cumulative_return:.2f}%\n")
                f.write(f"  质量分(收益*胜率): {attr.quality_score:.2f}\n\n")

            # 计算最优权重
            optimal_weights = self.calculate_optimal_weights(use_cumulative=True)

            f.write("=" * 60 + "\n")
            f.write("最优权重配置:\n")
            f.write("=" * 60 + "\n")
            for signal_type in sorted(optimal_weights.keys()):
                f.write(f"  {signal_type}: {optimal_weights[signal_type]}\n")

            f.write("\n" + "=" * 60 + "\n")
            f.write("建议操作:\n")
            f.write("=" * 60 + "\n")
            f.write("1. 手动编辑 config.yaml 应用权重\n")
            f.write("2. 或运行: python3 -m app.dynamic_weights --apply\n")

        logger.info(f"权重报告已导出: {output_file}")


# ===================== 命令行入口 =====================

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='动态权重优化工具')
    parser.add_argument('--file', '-f', default='signal_attribution.csv',
                       help='归因数据文件路径')
    parser.add_argument('--apply', '-a', action='store_true',
                       help='直接应用权重到config.yaml')
    parser.add_argument('--report', '-r', default='weight_report.txt',
                       help='导出权重报告到文件')
    parser.add_argument('--no-cumulative', action='store_true',
                       help='不使用累积收益作为基础分')

    args = parser.parse_args()

    optimizer = DynamicWeightOptimizer(args.file)

    if args.apply:
        weights = optimizer.calculate_optimal_weights(use_cumulative=not args.no_cumulative)
        applied = optimizer.apply_to_config('config.yaml', weights)
        print(f"✓ 已应用权重到 config.yaml")
        print(f"  配置路径: strategy.signal_ranking")
        print(f"  更新信号数: {len(applied)}")

    optimizer.export_weight_report(args.report)
    print(f"✓ 权重报告已导出: {args.report}")
