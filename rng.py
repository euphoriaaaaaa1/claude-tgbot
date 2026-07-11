"""真随机源封装。

所有需要随机决策的地方必须用这里的函数，不要用 Python 默认 random。
secrets.SystemRandom 基于 /dev/urandom（操作系统熵源），不可被状态推断。
"""
import secrets

_rng = secrets.SystemRandom()


def chance(p: float) -> bool:
    """以概率 p ∈ [0, 1] 返回 True。"""
    return _rng.random() < p


def uniform(lo: float, hi: float) -> float:
    """返回 [lo, hi] 之间的真随机浮点数。"""
    return _rng.uniform(lo, hi)


def choice(seq):
    """从序列中真随机挑一个元素。空序列时返回 None。"""
    if not seq:
        return None
    return _rng.choice(seq)
