from collections import deque
from collections.abc import Iterator
from math import ceil
from typing import Any

import numpy as np

from molflux.splits.bases import SplittingStrategyBase
from molflux.splits.info import SplittingStrategyInfo
from molflux.splits.typing import ArrayLike, SplitIndices, Splittable
from molflux.splits.utils import partition

_DESCRIPTION = """
Deterministic linear cross-validator with rotation of returned indices.
Useful for extremely small datasets not served by other strategies.
Given n_splits, a rotation of window = len(dataset)/n_splits is calculated.
Indices array is rotated by window after each split.
"""


class LinearSplitWithRotation(SplittingStrategyBase):
    def _info(self) -> SplittingStrategyInfo:
        return SplittingStrategyInfo(
            description=_DESCRIPTION,
        )

    def _split(
        self,
        dataset: Splittable,
        y: ArrayLike | None = None,
        groups: ArrayLike | None = None,
        *,
        n_splits: int = 1,
        train_fraction: float = 0.8,
        validation_fraction: float = 0.1,
        test_fraction: float = 0.1,
        **kwargs: Any,
    ) -> Iterator[SplitIndices]:
        """
        Args:
            dataset: The data to be split.
            y (optional): The target variable for supervised learning problems.
            groups (optional): Group labels for the samples used while splitting the dataset.
            n_splits (optional): The number of splits to generate. Defaults to 1.
            train_fraction (optional): The proportion of the dataset to include in the train split.
                Defaults to 0.8.
            validation_fraction: The proportion of the dataset to include in the validation split.
                Defaults to 0.1.
            test_fraction: The proportion of the dataset to include in the test split.
                Defaults to 0.1.
        """
        np.testing.assert_almost_equal(
            train_fraction + validation_fraction + test_fraction,
            1.0,
        )
        window = ceil(len(dataset) / n_splits)

        train_cutoff, validation_cutoff = partition(
            dataset,
            train_fraction,
            validation_fraction,
        )
        indices = np.array(range(len(dataset)))
        dequeued_indices = deque(indices)

        for _ in range(n_splits):
            train_indices, validation_indices, test_indices = np.split(
                list(dequeued_indices),
                [train_cutoff, validation_cutoff],
            )
            dequeued_indices.rotate(window)
            yield train_indices, validation_indices, test_indices
