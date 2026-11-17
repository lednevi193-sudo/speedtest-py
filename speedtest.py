#!/usr/bin/env python3
"""Замер скорости скачивания: N последовательных GET-запросов к одному URL.

Единицы: размер в МБ (10^6 байт), скорость в Мбит/с (10^6 бит/с) и МБ/с.
Подробности методики — в README.md.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Sample:
    status: int
    size: int        # байты тела, полученные по сети (до распаковки gzip/br)
    total_s: float   # полное время запроса: соединение + ожидание ответа + тело
    ttfb_s: float    # до получения заголовков ответа
    body_s: float    # передача тела


def measure(client: httpx.Client, url: str) -> Sample:
    """Один запрос: тело читается стримом и сразу выбрасывается, в память не копится."""
    start = time.perf_counter()
    with client.stream("GET", url) as response:
        headers_at = time.perf_counter()
        response.raise_for_status()
        # Без chunk_size httpx отдаёт куски в том виде, в каком их прочитал транспорт
        # (до 64 КБ, по HTTPS обычно ~16 КБ), без склейки в промежуточном буфере.
        for _ in response.iter_raw():
            pass
        end = time.perf_counter()
        return Sample(
            status=response.status_code,
            size=response.num_bytes_downloaded,
            total_s=end - start,
            ttfb_s=headers_at - start,
            body_s=end - headers_at,
        )


def mbit_per_s(size: int, seconds: float) -> float:
    return size * 8 / seconds / 1e6 if seconds > 0 else float("nan")


def mb(size: int) -> float:
    return size / 1e6


# Колонки построчного вывода результатов: по строке на каждый запрос, над ними заголовок.
# Элемент — (заголовок колонки, ширина в символах). Ширины заданы только здесь:
# и заголовок, и строки с результатами выравниваются по ним через format_row().
COLUMNS = [
    ("#", 3),
    ("статус", 6),
    ("размер, МБ", 10),
    ("время, с", 8),
    ("до ответа, с", 12),
    ("Мбит/с", 8),
]


def format_row(cells: list[str]) -> str:
    """Выравнивает ячейки вправо по ширинам из COLUMNS."""
    return "  ".join(cell.rjust(width) for cell, (_, width) in zip(cells, COLUMNS))


def sample_cells(number: int, s: Sample) -> list[str]:
    return [
        str(number),
        str(s.status),
        f"{mb(s.size):.2f}",
        f"{s.total_s:.3f}",
        f"{s.ttfb_s:.3f}",
        f"{mbit_per_s(s.size, s.total_s):.2f}",
    ]


def print_summary(samples: list[Sample]) -> None:
    total_bytes = sum(s.size for s in samples)
    total_time = sum(s.total_s for s in samples)
    body_time = sum(s.body_s for s in samples)
    times = [s.total_s for s in samples]

    speed = mbit_per_s(total_bytes, total_time)
    body_speed = mbit_per_s(total_bytes, body_time)

    print()
    print("Итого")
    print(f"  Запросов:                  {len(samples)}")
    print(f"  Скачано:                   {mb(total_bytes):.2f} МБ ({total_bytes:,} байт)".replace(",", " "))
    print(f"  Среднее время запроса:     {statistics.fmean(times):.3f} с "
          f"(медиана {statistics.median(times):.3f}, мин {min(times):.3f}, макс {max(times):.3f})")
    print(f"  Среднее время до ответа:   {statistics.fmean(s.ttfb_s for s in samples):.3f} с")
    print(f"  Скорость скачивания:       {speed:.2f} Мбит/с ({speed / 8:.2f} МБ/с)")
    print(f"  Скорость передачи тела:    {body_speed:.2f} Мбит/с ({body_speed / 8:.2f} МБ/с)")


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("должно быть >= 1")
    return n


def non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("должно быть >= 0")
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Делает N последовательных запросов к URL и печатает скорость скачивания.")
    parser.add_argument("url", help="адрес ресурса, например тяжёлой картинки")
    parser.add_argument("-n", "--count", type=positive_int, default=10,
                        help="число замеряемых запросов (по умолчанию 10)")
    parser.add_argument("--warmup", type=non_negative_int, default=1,
                        help="прогревочные запросы до замера, не входят в статистику: "
                             "открывают соединение и прогревают кеши (по умолчанию 1)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="таймаут на соединение и на каждое чтение из сокета, с (по умолчанию 30)")
    args = parser.parse_args(argv)
    if not args.url.lower().startswith(("http://", "https://")):
        parser.error("URL должен начинаться с http:// или https://")
    return args


def force_utf8_output() -> None:
    """На Windows при перенаправлении вывода (pipe, файл) Python берёт ANSI-кодировку
    (cp1252/cp1251) и падает на кириллице. Принудительно пишем UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    args = parse_args(argv)
    print(f"URL: {args.url}")
    print(f"Запросов: {args.count}, прогревочных: {args.warmup}, соединение переиспользуется")
    print()
    print(format_row([title for title, _ in COLUMNS]))

    samples: list[Sample] = []
    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
            for _ in range(args.warmup):
                measure(client, args.url)
            for i in range(1, args.count + 1):
                s = measure(client, args.url)
                samples.append(s)
                print(format_row(sample_cells(i, s)), flush=True)
    except httpx.HTTPStatusError as e:
        print(f"Ошибка: сервер ответил {e.response.status_code} {e.response.reason_phrase}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"Ошибка запроса: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        if not samples:
            return 130
        print_summary(samples)
        return 130

    print_summary(samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
