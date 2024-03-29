# From https://github.com/commaai/openpilot/blob/7638572e38773cdc5ecbf804ffc733fa9dd7893f/common/realtime.py#L48
import gc
import os
import time
from collections import deque
from typing import List, Optional, Union

from setproctitle import getproctitle


class Ratekeeper:
    def __init__(
        self, rate: float, print_delay_threshold: Optional[float] = 0.0, name=""
    ) -> None:
        """Rate in Hz for ratekeeping. print_delay_threshold must be nonnegative."""
        self._interval = 1.0 / rate
        self._next_frame_time = time.monotonic() + self._interval
        self._print_delay_threshold = print_delay_threshold
        self._frame = 0
        self._remaining = 0.0
        self._process_name = name + " " + getproctitle()
        self._dts = deque([self._interval], maxlen=100)
        self._last_monitor_time = time.monotonic()

    @property
    def frame(self) -> int:
        return self._frame

    @property
    def remaining(self) -> float:
        return self._remaining

    @property
    def lagging(self) -> bool:
        avg_dt = sum(self._dts) / len(self._dts)
        expected_dt = self._interval * (1 / 0.9)
        return avg_dt > expected_dt

    # Maintain loop rate by calling this at the end of each loop
    def keep_time(self) -> bool:
        lagged = self.monitor_time()
        if self._remaining > 0:
            time.sleep(self._remaining)
        return lagged

    # this only monitor the cumulative lag, but does not enforce a rate
    def monitor_time(self) -> bool:
        prev = self._last_monitor_time
        self._last_monitor_time = time.monotonic()
        self._dts.append(self._last_monitor_time - prev)

        lagged = False
        remaining = self._next_frame_time - time.monotonic()
        self._next_frame_time += self._interval
        if (
            self._print_delay_threshold is not None
            and remaining < -self._print_delay_threshold
        ):
            print(f"{self._process_name} lagging by {-remaining * 1000:.2f} ms")
            lagged = True
        self._frame += 1
        self._remaining = remaining
        return lagged
