import ccxt
from ccxt_rate_limiter import scale_limits, wrap_object
from ccxt_rate_limiter.ftx import ftx_limits, ftx_wrap_defs
from ccxt_rate_limiter.rate_limiter_group import RateLimiterGroup
from crypto_data_fetcher.ftx import FtxFetcher
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

EXECUTION_LAG_SEC = 0
EXECUTION_TIME_SEC = 60 * 60
EXECUTION_INTERVAL_SEC = 24 * 60 * 60


def fetch_ohlcv(
    symbols: list,
    with_target=False,
    interval_sec=60 * 60,
    logger=None,
    price_type="index",
    horizon=24
):
    dfs = []
    for symbol in symbols:
        fetcher = create_data_fetcher(logger=logger)
        df = fetcher.fetch_ohlcv(
            df=None,
            start_time=None,
            interval_sec=interval_sec,
            market=symbol + "-PERP",
            price_type=price_type,
        )
        df = df.reset_index()

        df["symbol"] = symbol
        df["execution_start_at"] = df["timestamp"] + pd.to_timedelta(
            interval_sec + EXECUTION_LAG_SEC, unit="S"
        )
        df = df.set_index(["timestamp", "symbol"])
        dfs.append(df)
    df = pd.concat(dfs)

    if with_target:
        df_target = _fetch_targets(symbols=symbols, logger=logger, horizon=horizon)
        df = df.reset_index().merge(
            df_target.reset_index(), on=["symbol", "execution_start_at"], how="left"
        )
        df = df.set_index(["timestamp", "symbol"])

    df = df.sort_index()
    return df


def _fetch_targets(symbols: list, logger=None, horizon=24):
    dfs = []
    for symbol in symbols:
        fetcher = create_data_fetcher(logger=logger)
        df = fetcher.fetch_ohlcv(
            df=None,
            start_time=None,
            interval_sec=300,
            market=symbol + "-PERP",
            price_type="index",
        )

        df = df.reset_index()

        shift = pd.to_timedelta(EXECUTION_LAG_SEC, unit="S")
        df["execution_start_at"] = (df["timestamp"] - shift).dt.floor(
            "{}S".format(EXECUTION_TIME_SEC)
        ) + shift
        df = pd.concat(
            [
                df.groupby(["execution_start_at"])["cl"].mean().rename("twap"),
            ],
            axis=1,
        )
        df["ret"] = (
            df["twap"].shift(-horizon // (EXECUTION_TIME_SEC // (60 * 60))) / df["twap"] - 1
        )
        df = df.drop(columns="twap")
        df = df.dropna()

        df = df.reset_index()
        df["symbol"] = symbol
        df = df.set_index(["execution_start_at", "symbol"])

        dfs.append(df)

    df = pd.concat(dfs)
    return df


def create_data_fetcher(logger=None):
    ftx_rate_limiter = RateLimiterGroup(limits=scale_limits(ftx_limits(), 0.5))
    client = ccxt.ftx(
        {
            "enableRateLimit": False,
        }
    )
    wrap_object(client, rate_limiter_group=ftx_rate_limiter, wrap_defs=ftx_wrap_defs())
    return FtxFetcher(ccxt_client=client, logger=logger)


def unbiased_rank(x):
    count = x.transform("count")
    rank = x.rank()
    return (rank - 1.0).mask(count == 1, rank - 0.5) / (count - 1.0).mask(
        count == 1, 1.0
    )


def ewm_finite(x, com, periods):
    y = x.copy()
    alpha = 1.0 / (1.0 + com)
    for i in range(1, periods):
        y += x.shift(i).fillna(0) * ((1 - alpha) ** i)
    return y


def _calc_cv_indicies(df, cv=5, causal=False):
    cv_indicies = []
    timestamps = df.index.get_level_values("timestamp").unique().sort_values()
    for i in range(cv):
        if causal and i == 0:
            continue

        start = i * timestamps.size // cv
        end = (i + 1) * timestamps.size // cv
        val_timestamps = timestamps[start:end]
        val_idx = df.loc[
            df.index.get_level_values("timestamp").isin(val_timestamps)
        ].index

        val_target_start_at = df.loc[val_idx, "execution_start_at"].min()
        val_target_end_at = df.loc[
            val_idx, "execution_start_at"
        ].max() + pd.to_timedelta(EXECUTION_TIME_SEC + EXECUTION_INTERVAL_SEC, unit="S")

        idx = (df["execution_start_at"]
            + pd.to_timedelta(EXECUTION_TIME_SEC + EXECUTION_INTERVAL_SEC, unit="S")
            < val_target_start_at
               )
        if not causal:
            idx = idx | (df.index.get_level_values("timestamp") > val_target_end_at)
        train_idx = df.loc[idx].index
        cv_indicies.append((train_idx, val_idx))

    return cv_indicies


def get_feature_columns(df):
    return sorted(df.columns[df.columns.str.startswith("feature")].to_list())


def get_symbols(df):
    return sorted(df.index.get_level_values("symbol").unique().to_list())


def normalize_position(df):
    df2 = df.copy()
    df2["position_abs"] = df2["position"].abs()
    df["position"] /= 1e-37 + df2.groupby("timestamp")["position_abs"].transform("sum")


def calc_position_cv(model, df, cv=5, causal=False):
    cv_indicies = _calc_cv_indicies(df, cv, causal=causal)
    for train_idx, val_idx in cv_indicies:
        model.fit(df.loc[train_idx])
        df.loc[val_idx, "position"] = model.predict(df.loc[val_idx])


# fixed version of calc_position_cv
# Problem:
# The original calc_position_cv destructively assigns position to df,
# so if dropna is performed in training, the training data will be dropped,
# which causes problems.
# Solution: not destroy input df
def calc_position_cv2(model, df, cv=5, causal=False):
    df_output = df.copy()
    cv_indicies = _calc_cv_indicies(df, cv, causal=causal)
    for train_idx, val_idx in cv_indicies:
        model.fit(df.loc[train_idx])
        df_output.loc[val_idx, "position"] = model.predict(df.loc[val_idx])
    return df_output


def calc_sharpe(x):
    return np.mean(x) / (1e-37 + np.std(x))


def calc_double_sharpe(x, window):
    agg = pd.Series(x).rolling(window)
    m = agg.mean()
    s = agg.std()
    sharpe = (m / s).dropna()
    return calc_sharpe(sharpe)


def calc_max_dd(x):
    return (x.expanding().max() - x).max()


def visualize_result(df, execution_cost=0.001):
    df = df.copy()

    # calc return
    df["ret_pos"] = df["ret"] * df["position"]
    df["hour"] = df.index.get_level_values("timestamp").hour
    df["position_prev"] = df.groupby(["hour", "symbol"])["position"].shift(1).fillna(0)
    df["cost"] = (df["position"] - df["position_prev"]).abs() * execution_cost
    df["ret_pos_cost"] = df["ret_pos"] - df["cost"]

    # print statistics
    for with_cost in [False, True]:
        if with_cost:
            print("return with cost statistics")
            x = df.groupby("timestamp")["ret_pos_cost"].sum()
        else:
            print("return without cost statistics")
            x = df.groupby("timestamp")["ret_pos"].sum()

        print("mean {}".format(np.mean(x)))
        print("std {}".format(np.std(x)))
        print("sharpe {}".format(calc_sharpe(x)))
        print("double sharpe {}".format(calc_double_sharpe(x, 24 * 30)))
        print("max drawdown {}".format(calc_max_dd(x)))

    # plot ret
    for symbol, df_symbol in df.groupby("symbol"):
        df_symbol = df_symbol.reset_index().set_index("timestamp")
        df_symbol["ret_pos_cost"].cumsum().plot(label=symbol)
    plt.legend(bbox_to_anchor=(1.05, 1))
    plt.title("return with cost by symbol")
    plt.show()

    # plot position
    for symbol, df_symbol in df.groupby("symbol"):
        df_symbol = df_symbol.reset_index().set_index("timestamp")
        df_symbol["position"].plot(label=symbol)
    plt.legend(bbox_to_anchor=(1.05, 1))
    plt.title("position by symbol")
    plt.show()

    # plot total ret
    df.groupby("timestamp")["ret_pos"].sum().cumsum().plot(label="ret without cost")
    df.groupby("timestamp")["ret_pos_cost"].sum().cumsum().plot(label="ret with cost")
    df.groupby("timestamp")["cost"].sum().cumsum().plot(label="cost")
    plt.legend(bbox_to_anchor=(1.05, 1))
    plt.title("total return")
    plt.show()
