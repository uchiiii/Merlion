#
# Copyright (c) 2022 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
Window Statistics anomaly detection model for data with weekly seasonality.
"""
import datetime
import logging

import numpy
import pandas as pd

from merlion.evaluate.anomaly import TSADMetric
from merlion.models.anomaly.base import DetectorConfig, DetectorBase
from merlion.post_process.threshold import AggregateAlarms
from merlion.transform.moving_average import DifferenceTransform
from merlion.utils import UnivariateTimeSeries, TimeSeries

logger = logging.getLogger(__name__)


class WindStatsConfig(DetectorConfig):
    """
    Config class for `WindStats`.
    """

    _default_transform = DifferenceTransform()

    @property
    def _default_threshold(self):
        t = 3.0 if self.enable_calibrator else 8.8
        return AggregateAlarms(
            alm_threshold=t, alm_window_minutes=self.wind_sz, alm_suppress_minutes=120, min_alm_in_window=1
        )

    def __init__(self, wind_sz=30, max_day=4, **kwargs):
        """
        :param wind_sz: the window size in minutes, default is 30 minute window
        :param max_day: maximum number of week days stored in memory (only mean
            and std of each window are stored). Here, the days are first
            bucketed by weekday and then by window id.
        """
        self.wind_sz = wind_sz
        self.max_day = max_day
        super().__init__(**kwargs)


class WindStats(DetectorBase):
    """
    Sliding Window Statistics based Anomaly Detector.
    This detector assumes the time series comes with a weekly seasonality.
    It divides the week into buckets of the specified size (in minutes). For
    a given (t, v) it computes an anomaly score by comparing the current
    value v against the historical values (mean and standard deviation) for
    that window of time.
    Note that if multiple matches (specified by the parameter max_day) can be
    found in history with the same weekday and same time window, then the
    minimum of the scores is returned.
    """

    config_class = WindStatsConfig

    def __init__(self, config: WindStatsConfig = None):
        """
        config.wind_sz: the window size in minutes, default is 30 minute window
        config.max_days: maximum number of week days stored in memory (only mean and std of each window are stored)
        here the days are first bucketized by weekday and then bucketized by window id.
        """
        super().__init__(WindStatsConfig() if config is None else config)
        self.table = {}

    @property
    def require_even_sampling(self) -> bool:
        return False

    @property
    def require_univariate(self) -> bool:
        return True

    @property
    def _default_post_rule_train_config(self):
        return dict(metric=TSADMetric.F1, unsup_quantile=None)

    def _get_anomaly_score(self, time_series: pd.DataFrame, time_series_prev: pd.DataFrame = None) -> pd.DataFrame:
        times, scores = [], []
        for t, (x,) in zip(time_series.index, time_series.values):
            t = t.timetuple()
            key = (t.tm_wday, (t.tm_hour * 60 + t.tm_min) // self.config.wind_sz)
            if key in self.table:
                stats = self.table[key]
                score = []
                for d, mu, sigma in stats:
                    if sigma == 0:
                        score.append(0)
                    else:
                        score.append((x - mu) / sigma)
            else:
                score = [0]
            scores.append(min(score, key=abs))

        return pd.DataFrame(scores, index=time_series.index)

    def _train(self, train_data: pd.DataFrame, train_config=None) -> pd.DataFrame:
        # first build a hashtable with (weekday, yearofday, and window id of the day) as key.
        # the value is a list of metrics
        table = {}
        for time, x in zip(train_data.index, train_data.values):
            t = time.timetuple()
            code = (t.tm_wday, t.tm_yday, (t.tm_hour * 60 + t.tm_min) // self.config.wind_sz)
            if code in table:
                table[code].append(x)
            else:
                table[code] = [x]

        # for each bucket, compute the mean and standard deviation
        for t, x in table.items():
            wd, d, h = t
            key = (wd, h)
            v1 = numpy.array(x)
            mu = numpy.mean(v1)
            sigma = numpy.std(v1)
            if key in self.table:
                self.table[key].append((d, mu, sigma))
            else:
                self.table[key] = [(d, mu, sigma)]

        # cut out maximum number of days saved in the table. only store the latest max_day
        for t, x in self.table.items():
            self.table[t] = sorted(x, key=lambda x: x[0])
            if len(self.table[t]) > self.config.max_day:
                self.table[t] = self.table[t][-self.config.max_day :]

        return self._get_anomaly_score(train_data)
