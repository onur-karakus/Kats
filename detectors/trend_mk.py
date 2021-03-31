#!/usr/bin/env python3

import datetime
import logging
from collections import Counter
from datetime import timedelta
from typing import Dict, List, NewType, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from infrastrategy.kats.consts import TimeSeriesChangePoint, TimeSeriesData
from infrastrategy.kats.detector import Detector
from scipy.stats import norm  # @manual
from statsmodels.tsa.api import SimpleExpSmoothing


# Constants
dt = NewType("dt", datetime.datetime)  # create a new typing type for datetime

pd.options.plotting.matplotlib.register_converters = True


class MKMetadata:
    def __init__(self, is_multivariate: bool, trend_direction: str, Tau: float or Dict):
        self._detector_type = MKDetector
        self._is_multivariate = is_multivariate
        self._trend_direction = trend_direction
        self._Tau = Tau

    @property
    def detector_type(self):
        return self._detector_type

    @property
    def is_multivariate(self):
        return self._is_multivariate

    @property
    def trend_direction(self):
        return self._trend_direction

    @property
    def Tau(self):
        return self._Tau  # Tau is a dict in multivariate case

    def __str__(self):
        return (
            f"MKDetector(detector_type: {self.detector_type},"
            f"is_multivariate: {self.is_multivariate},"
            f"trend_direction: {self.trend_direction}, Tau: {self.Tau})"
        )


class MKDetector(Detector):
    """
    MKDetector (MK stands for Mann-Kendall) is a non-parametric statistical test used to
    determine whether there is a monotonic trend in a given time series.
    See https://vsp.pnnl.gov/help/vsample/Design_Trend_Mann_Kendall.htm for details.

    The basic idea is to check whether there is a monotonic trend based on a
    look back number of time steps (window_size).

    Parameters:
        data: TimeSeriesData, this is time series data at one-day granularity.
            This time series can be either univariate or multivariate.
            We require more than training_days points in each time series.
        threshold: float, threshold for trend intensity; higher threshold gives
            trend with high intensity (0.8 by default)
        alpha: float, significance level (0.05 by default)s
        multivariate: bool, whether the input time series is multivariate

    Example:
    -------
        >>> import pandas as pd
        >>> from infrastrategy.kats.consts import TimeSeriesData
        >>> from infrastrategy.kats.detectors.trend_mk import MKDetector
        >>> # read data and rename the two columns required by TimeSeriesData structure
        >>> data = pd.read_csv("../filename.csv") # this file does not exist, just a demo
        >>> TSdata = TimeSeriesData(data) # check TimeSeriesData to see the requirements of data
        >>> # create MKDetector with given data and params
        >>> d = MKDetector(data=TSdata)
        >>> # call detector method to fit model
        >>> detected_time_points = d.detector(window_size=20, direction="up")
        >>> # plot the results
        >>> d.plot(detected_time_points)
        See more examples in notebook N330840.
    """

    def __init__(
        self,
        data: TimeSeriesData = None,
        threshold: float = 0.8,
        alpha: float = 0.05,
        multivariate: bool = False,
    ) -> None:
        super(MKDetector, self).__init__(data=data)

        self.threshold = threshold
        self.alpha = alpha
        self.multivariate = multivariate
        self.__subtype__ = 'trend_detector'

        # Assume univariate but multivariate data is detected
        if self.data is not None:
            if not self.data.is_univariate() and not self.multivariate:
                logging.warning(
                    "Your data is multivariate. A multivariate MK test will be performed."
                )
                self.multivariate = True
            # Assume multivariate but univariate data is detected
            elif self.data.is_univariate() and self.multivariate:
                logging.warning(
                    "Your data is univariate. A univariate MK test will be performed."
                )

    def _remove_seasonality(self, ts: pd.DataFrame, freq: str = None) -> pd.DataFrame:
        """ Remove seasonality in the time series using moving average. """
        if freq is None:
            return ts  # no seasonality
        else:
            map = {"weekly": 7, "monthly": 30, "yearly": 365}
            ts = ts.rolling(window=map[freq]).mean()
        return ts

    def _smoothing(self, ts: pd.DataFrame) -> pd.DataFrame:
        """ Remove noise in the time series using holt-winters model. """
        smoothed_ts = pd.DataFrame()
        for c in ts.columns:
            ts_c = ts[c].dropna()
            with np.errstate(divide="raise"):
                try:
                    model = SimpleExpSmoothing(ts_c)
                    _fit = model.fit(smoothing_level=0.2, optimized=False)
                    smoothed_ts_tmp = _fit.predict(
                        start=ts_c.index[0], end=ts_c.index[-1]
                    )
                    smoothed_ts = pd.concat(
                        [smoothed_ts, smoothed_ts_tmp.rename(c)], axis=1
                    )
                except FloatingPointError:
                    smoothed_ts = pd.concat([smoothed_ts, ts_c], axis=1)
                    logging.debug(
                        "Your data does not have noise. No need for smoothing"
                    )

        return smoothed_ts

    def _preprocessing(self, ts: pd.DataFrame) -> Tuple[np.array, int]:
        """ Check and convert the dataframe ts to an numpy array of length n.
            ts is a time series dataframe with time as index. """
        # takes only window_size days
        x = np.asarray(ts[-self.window_size :])
        dim = x.ndim

        # checks the dimension of the data
        if dim == 2:  # dim should always be 2
            (n, c) = x.shape  # n is # of obs = window_size, and c is # of metrics
            if c == 1:  # univariate case
                dim = 1  # converts x from 2-dim array (n, 1) to 1-dim array (n,)
                x = x.flatten()
        else:
            msg = f"dim = 2 is expected but your data has dim = {dim}."
            raise ValueError(msg)

        return x, c

    def _drop_missing_values(self, x: np.array) -> Tuple[np.array, int]:
        """ Drop the missing values in x. """
        if x.ndim == 1:  # univariate case with 1-dim array/ shape(n,)
            x = x[~(np.isnan(x))]
        else:  # multivariate case with 2-dim arrat/ shape (n, c)
            x = x[~np.isnan(x).any(axis=1)]
        return x, len(x)

    def _mk_score(self, x: np.array) -> float:
        """ Calculate the Mann-Kendall score s. """
        s = 0
        n = len(x)
        for k in range(n - 1):
            for j in range(k + 1, n):
                s += np.sign(x[j] - x[k])
        return s

    def _var_s(self, x: np.array) -> float:
        """ Calculate the Mann-Kendall's variance var_s. """
        n = len(x)
        unique_x = np.unique(x)
        if n == len(unique_x):  # there are no duplicated values
            var_s = (n * (n - 1) * (2 * n + 5)) / 18
        else:  # there are some duplicated values
            tp = np.array(list(Counter(np.array(x)).values()), dtype=float)
            var_s = (
                n * (n - 1) * (2 * n + 5) + np.sum(tp * (tp - 1) * (2 * tp + 5))
            ) / 18
        return var_s

    def _z_score(self, s: float, var_s: float) -> float:
        """ Calculate the normalized test statistics z. """
        if s > 0:
            z = (s - 1) / np.sqrt(var_s)
        elif s == 0:
            z = 0
        elif s < 0:
            z = (s + 1) / np.sqrt(var_s)
        return z

    def _p_value(self, z: float, Tau: float) -> Tuple[float, str]:
        """ Calculate the p-value of the significance test and tells the trend. """
        p = 2 * (1 - norm.cdf(abs(z)))  # p-value of the significance test
        h = abs(z) > norm.ppf(1 - self.alpha / 2)  # whether there exists a trend

        if (z < 0) and h and Tau < (-1 * self.threshold):
            trend = "decreasing"
        elif (z > 0) and h and Tau > self.threshold:
            trend = "increasing"
        else:
            trend = "no trend"

        return p, trend

    def MKtest(self, ts: pd.DataFrame) -> Tuple[dt, str, float, float]:
        """
        This functions performs the Mann-Kendall (MK) test for trend detection (Mann 1945,
            Kendall 1975, Gilbert 1987).
        Input:
            ts: the dataframe of input data with time as index.
                This time series should not present seasonality for MK test.
        Output:
            anchor_date: the last time point in ts; the date for which alert is triggered
            trend: tells the trend (decreasing, increasing, or no trend)
            p: p-value of the significance test
            Tau: Kendall Tau
        """
        x, _ = self._preprocessing(ts)
        x, n = self._drop_missing_values(x)

        anchor_date = ts.index[-1]

        # calculate s
        s = self._mk_score(x)

        # calculate var_s
        var_s = self._var_s(x)

        # calculate the z_score
        z = self._z_score(s, var_s)

        # calculate Tau
        Tau = s / (0.5 * n * (n - 1))

        # calculate the p_value and trend
        p, trend = self._p_value(z, Tau)

        return anchor_date, trend, p, Tau

    def multivariate_MKtest(self, ts: pd.DataFrame) -> Tuple[dt, str, float, Dict]:
        """
        This function performs the Multivariate Mann-Kendall (MK) test proposed by
        R. M. Hirsch and J. R. Slack (1984).
        Input:
            ts: the dataframe of input data with time as index.
                This time series should not present seasonality for MK test.
         Output:
            anchor_date: the last time point in ts; the date for which alert is triggered
            trend:_dict: tells the trend (decreasing, increasing, or no trend) for each metric
            p: p-value of the significance test
            Tau_dict: Kendall Tau for each metric
        """
        s = 0
        var_s = 0
        denominator = 0

        anchor_date = ts.index[-1]

        Tau_dict = {}  # this Tau_dict contains score for individual cluster and overall
        trend_dict = (
            {}
        )  # this trend_dict contains trend for individual cluster and overall

        x, c = self._preprocessing(ts)

        for i in range(c):
            x_i, n = self._drop_missing_values(x[:, i])
            s_i = self._mk_score(x_i)
            var_s_i = self._var_s(x_i)
            denominator_i = 0.5 * n * (n - 1)

            # individual Tau score and trend
            try:
                Tau_i = s_i / denominator_i
                z_i = self._z_score(s_i, var_s_i)
                p_i, trend_i = self._p_value(z_i, Tau_i)
                Tau_dict[ts.columns[i]] = Tau_i
                trend_dict[ts.columns[i]] = trend_i

            except ZeroDivisionError:
                Tau_dict[ts.columns[i]] = None
                trend_dict[ts.columns[i]] = None

            s = s + s_i
            var_s = var_s + var_s_i
            denominator = denominator + denominator_i

        Tau_dict["overall"] = s / denominator  # overall Tau score

        z = self._z_score(s, var_s)
        p, trend_dict["overall"] = self._p_value(z, Tau_dict["overall"])

        return anchor_date, trend_dict, p, Tau_dict

    def runDetector(self, ts: pd.DataFrame) -> Dict:
        """
        This function runs MK test for a time point in the input data,
            and saves its related statistics to a dict.
        Input:
            ts: the dataframe of input data with noise and seasonality removed.
                Its index is time.
        Output: a dictionary consists of MK test statistics for the anchor time point,
            including trend, p-value and Kendall Tau.
        """
        # run MK test
        if self.multivariate:
            anchor_date, trend, p, Tau = self.multivariate_MKtest(ts)
        else:
            anchor_date, trend, p, Tau = self.MKtest(ts)

        return {"ds": anchor_date, "trend_direction": trend, "p": p, "Tau": Tau}

    def detector(
        self,
        window_size: int = 20,
        training_days: int = None,
        direction: str = "both",
        freq: str = None,
    ) -> List[Tuple[TimeSeriesChangePoint, MKMetadata]]:
        """
        This function runs MK test sequentially. It finds the trend and calculates
            the related statistics for all time points in a given time series.
        Input:
            window_size: int, the number of look back days for checking trend
                persistence (20 days by default)

            training_days: int, the number of days for time series smoothing;
                should be greater or equal to window_size (None by default)
                If training_days is None, we will perform trend detection on the whole
                time series; otherwise, we will perform trend detection only for the
                anchor point using the previous training_days data.

            direction: string, the direction of the trend to be detected, choose from
                {"down", "up", "both"}  ("both" by default)

            freq: str, the type of seasonality shown in the time series,
                choose from {'weekly','monthly','yearly'} (None by default)
        """
        self.window_size = window_size
        self.training_days = training_days
        self.direction = direction
        self.freq = freq

        ts = self.data.to_dataframe().set_index("time")
        ts = ts.dropna(axis=1)
        ts.index = pd.DatetimeIndex(ts.index.values, freq=ts.index.inferred_freq)
        self.ts = ts

        if self.training_days is None:
            logging.info("Performing trend detection on the whole time series...")
            # check validity of the input value
            if len(ts) < self.window_size:
                raise ValueError(
                    f"For the whole time series analysis, data must have at least"
                    f"window_size={self.window_size} points."
                )

        else:
            logging.info(
                f"Performing trend detection for the anchor date {ts.index[-1]} with "
                f"training_days={self.training_days}..."
            )

            # check validity of the input value
            if self.training_days < self.window_size:
                raise ValueError(
                    f"For the anchor date analysis, training days should have at "
                    f"least window_size={self.window_size} points."
                )

            if len(ts) < self.training_days:
                raise ValueError(
                    f"For the anchor date analysis, data must have "
                    f"at least training_days={self.training_days} points."
                )

        # save the trend detection results to dataframe MK_statistics
        MK_statistics = pd.DataFrame(columns=["ds", "trend_direction", "p", "Tau"])

        if self.training_days is not None:  # anchor date analysis for real-time setting
            # only look back training_days for noise and seasonality removal
            ts = ts.loc[
                (ts.index[-1] - timedelta(days=self.training_days)) : ts.index[-1], :
            ]
            ts_deseas = self._remove_seasonality(ts, freq=self.freq)  # deseasonzation
            ts_smoothed = self._smoothing(ts_deseas)  # smoothing
            # append MK statistics to MK_statistics dataframe
            MK_statistics = MK_statistics.append(
                self.runDetector(ts=ts_smoothed), ignore_index=True
            )

        else:
            # use the whole time series for for noise and seasonality removal
            ts_deseas = self._remove_seasonality(ts, freq=self.freq)
            ts_smoothed = self._smoothing(ts_deseas)

            # run detector sequentially with sliding_window for the whole time series
            for t in ts_smoothed.index[
                self.window_size :
            ]:  # look back window_size day for trend detection
                ts_tmp = ts_smoothed.loc[:t, :]
                # append MK statistics to MK_statistics dataframe
                MK_statistics = MK_statistics.append(
                    self.runDetector(ts=ts_tmp), ignore_index=True
                )

        self.MK_statistics = MK_statistics

        # take the subset for detection with specified trend_direction
        MK_results = self.get_MK_results(
            MK_statistics=MK_statistics, direction=self.direction
        )

        return self._convert_detected_tps(MK_results)

    def get_MK_results(self, MK_statistics: pd.DataFrame, direction) -> pd.DataFrame:
        """ Obtain a subset of MK_statistics given the desired direction """

        if direction not in ["up", "down", "both"]:
            raise ValueError("direction should be chosen from {'up', 'down', 'both'}")

        if self.multivariate:
            trend_df = pd.DataFrame.from_dict(list(MK_statistics.trend_direction))
            overall_trend = trend_df["overall"]

            if direction == "down":
                MK_results = MK_statistics.loc[overall_trend == "decreasing", :]
            elif direction == "up":
                MK_results = MK_statistics.loc[overall_trend == "increasing", :]
            elif direction == "both":
                MK_results = MK_statistics.loc[overall_trend != "no trend", :]
        else:
            if direction == "down":
                MK_results = MK_statistics.loc[
                    MK_statistics["trend_direction"] == "decreasing", :
                ]
            elif direction == "up":
                MK_results = MK_statistics.loc[
                    MK_statistics["trend_direction"] == "increasing", :
                ]
            elif direction == "both":
                MK_results = MK_statistics.loc[
                    MK_statistics["trend_direction"] != "no trend", :
                ]
        return MK_results

    def _convert_detected_tps(
        self, MK_results: pd.DataFrame
    ) -> List[Tuple[TimeSeriesChangePoint, MKMetadata]]:
        """
        Convert the dataframe of detected_tps and Tau into desired format by Kat's convention
        """
        converted = []

        for _index, row in MK_results.iterrows():
            t = row["ds"]
            detected_time_point = TimeSeriesChangePoint(
                start_time=t, end_time=t, confidence=1 - row["p"]
            )

            metadata = MKMetadata(
                is_multivariate=self.multivariate,
                trend_direction=row["trend_direction"],
                Tau=row["Tau"],
            )
            converted.append((detected_time_point, metadata))

        return converted

    def get_MK_statistics(self) -> pd.DataFrame:
        """
        This function obtains dataframe MK_statistics.
        """
        return self.MK_statistics

    def get_top_k_metrics(self, time_point: dt, top_k: int = None) -> pd.DataFrame:
        """
        This function obtains k metrics that shows the most significant trend at a time point.
        Only works for multivariate data.
        Input:
            time_point: the time point to be investigated
            top_k: the number of top metrics
        Output:
            a dataframe consists of top_k metrics and their corresponding Kendall Tau and trend
        """
        Tau_df, trend_df = self._metrics_analysis()
        Tau_df = Tau_df.melt(id_vars=["ds"], var_name="metric", value_name="Tau")
        trend_df = trend_df.melt(
            id_vars=["ds"], var_name="metric", value_name="trend_direction"
        )

        if self.training_days is not None:
            time_point = self.data.time.iloc[-1]
            # time_point default to the only anchor date for real-time detection

        # obtain the Tau for all metrics at the time point
        Tau_df_tp = Tau_df.loc[Tau_df["ds"] == time_point, :]
        trend_df_tp = trend_df.loc[trend_df["ds"] == time_point, :]
        MK_statistics_tp = pd.merge(Tau_df_tp, trend_df_tp)

        # sort the metrics according to their Tau
        if self.direction == "down":
            top_metrics = MK_statistics_tp.reindex(
                MK_statistics_tp.Tau.sort_values(axis=0).index
            )
        elif self.direction == "up":
            top_metrics = MK_statistics_tp.reindex(
                MK_statistics_tp.Tau.sort_values(axis=0, ascending=False).index
            )
        elif self.direction == "both":
            top_metrics = MK_statistics_tp.reindex(
                MK_statistics_tp.Tau.abs().sort_values(axis=0, ascending=False).index
            )

        if top_k is None:
            return (
                top_metrics  # if top_k not specified, return all metrics ranked by Tau
            )

        return top_metrics.iloc[:top_k]

    def plot_heat_map(self) -> pd.DataFrame:
        """
        This function plots the Tau of each metric in a heatmap
        Output: a dataframe contains Tau for all metrics at all time points
        """
        import plotly.graph_objects as go

        Tau_df, _ = self._metrics_analysis()
        Tau_df = Tau_df.set_index("ds")

        fig = go.Figure(
            data=go.Heatmap(
                z=Tau_df.T.values,
                x=Tau_df.index,
                y=Tau_df.columns,
                colorscale="Viridis",
                reversescale=True,
            )
        )

        fig.update_layout(
            xaxis_title="time",
            yaxis_title="value",
            xaxis={"title": "time", "tickangle": 45},
        )

        fig.show()

        return Tau_df

    def _metrics_analysis(self) -> pd.DataFrame:
        if not self.multivariate:
            raise ValueError("Your data is not multivariate.")

        # obtain the Tau for all metrics at all time points
        Tau_df = pd.DataFrame.from_dict(list(self.MK_statistics.Tau))
        Tau_df["ds"] = self.MK_statistics.ds
        Tau_df = Tau_df.drop(["overall"], axis=1)  # remove overall score

        trend_df = pd.DataFrame.from_dict(list(self.MK_statistics.trend_direction))
        trend_df["ds"] = self.MK_statistics.ds
        trend_df = trend_df.drop(["overall"], axis=1)  # remove overall trend

        return Tau_df, trend_df

    def plot(
        self, detected_time_points: List[Tuple[TimeSeriesChangePoint, MKMetadata]]
    ) -> None:
        """
        This function plots the original time series data, and the detected time points.
        """
        plt.figure(figsize=(14, 5))

        plt.plot(self.ts.index, self.ts.values)

        if len(detected_time_points) == 0:
            logging.warning("No trend detected!")

        for t in detected_time_points:
            plt.axvline(x=t[0].start_time, color="red")