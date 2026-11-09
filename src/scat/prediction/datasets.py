"""M6-03 按航班划分数据集：避免同一航班泄漏进多个子集。

两种划分方式（D-05 的最终策略待模型阶段确定，这里先提供可复现的默认实现）：

- `split_flights_by_flight`：按 external_id 整体随机划分（默认 70/15/15）；
- `split_flights_by_time`：按参考时刻切分，供时间外推实验使用。

确定性：同一 `seed`/同一输入 → 同一划分；各子集输出均排序，便于比对与记录版本。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from random import Random

__all__ = [
    "FlightSplit",
    "split_flights_by_flight",
    "split_flights_by_time",
]


@dataclass(frozen=True, slots=True)
class FlightSplit:
    """航班级数据集划分：三个子集互不相交。"""

    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]

    @property
    def all_flights(self) -> tuple[str, ...]:
        return tuple(sorted((*self.train, *self.validation, *self.test)))


def split_flights_by_flight(
    external_ids: Iterable[str | int],
    *,
    seed: int,
    train_ratio: float = 0.7,
    validation_ratio: float = 0.15,
) -> FlightSplit:
    """按航班整体随机划分（先 shuffle 再切段）；不足 3 个航班或重复 id 时报错。"""
    ids = [str(value) for value in external_ids]
    if len(set(ids)) != len(ids):
        raise ValueError("external_id 存在重复")
    if train_ratio <= 0 or validation_ratio < 0 or train_ratio + validation_ratio >= 1:
        raise ValueError("比例非法：要求 train>0、validation≥0，且二者之和 < 1")
    count = len(ids)
    if count < 3:
        raise ValueError("至少需要 3 个航班才能划分 train/validation/test")

    shuffled = ids[:]
    Random(seed).shuffle(shuffled)
    n_validation = max(1, round(count * validation_ratio)) if validation_ratio > 0 else 0
    n_test = max(1, round(count * (1 - train_ratio - validation_ratio)))
    n_train = count - n_validation - n_test
    if n_train < 1:
        raise ValueError("比例划分后训练集为空，请调整比例或增加航班数")

    train = shuffled[:n_train]
    validation = shuffled[n_train : n_train + n_validation]
    test = shuffled[n_train + n_validation :]
    return FlightSplit(tuple(sorted(train)), tuple(sorted(validation)), tuple(sorted(test)))


def split_flights_by_time(
    flights: Iterable[tuple[str | int, datetime]],
    *,
    train_val_boundary: datetime,
    val_test_boundary: datetime,
) -> FlightSplit:
    """按参考时刻切分：train < 边界一 ≤ validation < 边界二 ≤ test（边界必须递增）。"""
    if train_val_boundary >= val_test_boundary:
        raise ValueError("边界必须递增：train_val_boundary < val_test_boundary")

    train: list[str] = []
    validation: list[str] = []
    test: list[str] = []
    seen: set[str] = set()
    for value, moment in flights:
        external_id = str(value)
        if external_id in seen:
            raise ValueError("external_id 存在重复")
        seen.add(external_id)
        if moment < train_val_boundary:
            train.append(external_id)
        elif moment < val_test_boundary:
            validation.append(external_id)
        else:
            test.append(external_id)

    if not train or not validation or not test:
        raise ValueError("至少一个子集为空：请检查边界时刻与航班时间范围")
    return FlightSplit(tuple(sorted(train)), tuple(sorted(validation)), tuple(sorted(test)))
