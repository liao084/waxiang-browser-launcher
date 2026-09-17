"""提供简单滑块所需的 CSS 距离计算、轨迹生成与鼠标执行工具。"""

from __future__ import annotations

import asyncio
import math
import random
from typing import NamedTuple, Sequence

from playwright.async_api import Page


_SAMPLE_INTERVAL_SECONDS = 0.0167
_MIN_RANDOM_POINT_COUNT = 49
_MAX_RANDOM_POINT_COUNT = 74


class TrajectoryPoint(NamedTuple):
    """表示相对于鼠标按下点的一个轨迹采样点。"""

    elapsed_seconds: float
    x: float
    y: float


def calculate_slider_drag_distance(
    sliding_region_width: float,
    slider_width: float,
) -> float:
    """按照滑动区域宽度减去滑块半宽计算 CSS 拖动距离。"""

    sliding_region_width = float(sliding_region_width)
    slider_width = float(slider_width)
    if not math.isfinite(sliding_region_width) or sliding_region_width <= 0:
        raise ValueError("sliding_region_width 必须是正的有限数值")
    if not math.isfinite(slider_width) or slider_width <= 0:
        raise ValueError("slider_width 必须是正的有限数值")

    distance_x = sliding_region_width - slider_width / 2
    if distance_x <= 0:
        raise ValueError(
            "滑动区域宽度必须大于滑块宽度的一半："
            f"sliding_region_width={sliding_region_width:.2f}, "
            f"slider_width={slider_width:.2f}"
        )
    return distance_x


def generate_drag_trajectory(
    distance_x: float,
    *,
    duration_seconds: float | None = None,
    vertical_amplitude: float = 6.0,
    point_count: int | None = None,
    random_seed: int | str | bytes | bytearray | None = None,
) -> list[TrajectoryPoint]:
    """生成 Minimum Jerk 时间进度下的三次贝塞尔拖动轨迹。

    返回点使用相对于鼠标按下位置的局部 CSS 坐标。默认随机生成
    49～74 个采样点，并按照每段约 16.7ms 推导总耗时。
    """

    distance_x = float(distance_x)
    vertical_amplitude = float(vertical_amplitude)
    if not math.isfinite(distance_x):
        raise ValueError("distance_x 必须是有限数值")
    if not math.isfinite(vertical_amplitude) or vertical_amplitude < 0:
        raise ValueError("vertical_amplitude 必须是非负有限数值")

    if duration_seconds is not None:
        duration_seconds = float(duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds 必须是正的有限数值")

    if point_count is not None:
        if isinstance(point_count, bool) or not isinstance(point_count, int):
            raise TypeError("point_count 必须是整数")
        if point_count < 2:
            raise ValueError("point_count 至少为 2")

    random_source = random.Random(random_seed)
    if point_count is None:
        if duration_seconds is None:
            point_count = random_source.randint(
                _MIN_RANDOM_POINT_COUNT,
                _MAX_RANDOM_POINT_COUNT,
            )
        else:
            point_count = max(
                2,
                round(duration_seconds / _SAMPLE_INTERVAL_SECONDS) + 1,
            )

    if duration_seconds is None:
        duration_seconds = round(
            (point_count - 1) * _SAMPLE_INTERVAL_SECONDS,
            1,
        )

    control_1_x = distance_x * random_source.uniform(0.2, 0.4)
    control_2_x = distance_x * random_source.uniform(0.6, 0.8)

    if vertical_amplitude == 0:
        control_1_y = 0.0
        control_2_y = 0.0
        end_y = 0.0
    else:
        direction = random_source.choice((-1.0, 1.0))
        control_1_y = (
            direction
            * vertical_amplitude
            * random_source.uniform(0.35, 0.75)
        )
        end_y_limit = min(1.5, vertical_amplitude * 0.3)
        end_y = random_source.uniform(-end_y_limit, end_y_limit)
        control_2_y = (
            direction
            * vertical_amplitude
            * random_source.uniform(0.35, 0.75)
        )

    points: list[TrajectoryPoint] = []
    for index in range(point_count):
        normalized_time = index / (point_count - 1)
        progress = (
            10 * normalized_time**3
            - 15 * normalized_time**4
            + 6 * normalized_time**5
        )
        inverse = 1 - progress

        x = (
            3 * inverse**2 * progress * control_1_x
            + 3 * inverse * progress**2 * control_2_x
            + progress**3 * distance_x
        )
        y = (
            3 * inverse**2 * progress * control_1_y
            + 3 * inverse * progress**2 * control_2_y
            + progress**3 * end_y
        )
        points.append(
            TrajectoryPoint(
                elapsed_seconds=normalized_time * duration_seconds,
                x=x,
                y=y,
            )
        )

    points[0] = TrajectoryPoint(0.0, 0.0, 0.0)
    points[-1] = TrajectoryPoint(duration_seconds, distance_x, end_y)
    return points


async def drag_mouse_along_trajectory(
    page: Page,
    start_x: float,
    start_y: float,
    trajectory: Sequence[TrajectoryPoint],
) -> None:
    """按轨迹绝对时间表拖动鼠标，并确保按下后最终释放左键。"""

    if len(trajectory) < 2:
        raise ValueError("trajectory 至少需要两个采样点")

    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    try:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        for point in trajectory[1:]:
            remaining_seconds = (
                started_at + point.elapsed_seconds - loop.time()
            )
            if remaining_seconds > 0:
                await asyncio.sleep(remaining_seconds)
            await page.mouse.move(start_x + point.x, start_y + point.y)
    finally:
        await page.mouse.up()
