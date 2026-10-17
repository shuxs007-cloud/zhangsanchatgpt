"""Independent row-major recomputation, never imports the aggregation code."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re
import unicodedata


def review_device_daily_statistics(book, source_headers, source_rows):
    title = "设备每日统计"
    if title not in book.sheetnames:
        return {"matches": False, "reason": "设备每日统计工作表缺失"}
    headers = [str(value or "").strip() for value in source_headers]
    if headers.count("卡槽") != 1:
        return {"matches": False, "reason": "源表卡槽列不唯一"}
    slot_column = headers.index("卡槽")
    metrics = []
    for index, header in enumerate(headers):
        found = re.fullmatch(r"(\d\d-\d\d)(发送窗口数|回复窗口数|回复率|总发送量|总回复量)", header)
        if found:
            metrics.append((index, found[1], found[2]))
    dates = list(dict.fromkeys(day for _, day, _ in metrics))
    schema_ok = bool(dates) and all(
        sorted(field for _, date_key, field in metrics if date_key == day)
        == sorted(("发送窗口数", "回复窗口数", "回复率", "总发送量", "总回复量")) for day in dates)
    sizes, sums, unknown = Counter(), {}, set()
    source_count = 0
    for values in source_rows:
        source_count += 1
        slot = unicodedata.normalize("NFKC", str(values[slot_column] or "")).strip().replace(" ", "").replace("-", "_")
        match = re.fullmatch(r"([A-Za-z0-9]+)_(\d+)(?:\.\d+)?", slot)
        key = (str(int(match[1])) if match[1].isdigit() else match[1]) if match else "未识别设备"
        sizes[key] += 1
        for column, day, field in metrics:
            if field == "回复率":
                continue
            identity = key, day, field
            value = values[column]
            try:
                if isinstance(value, bool) or value is None:
                    raise ValueError()
                numeric = Decimal(str(value).strip().replace(",", ""))
                if not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value():
                    raise ValueError()
            except (InvalidOperation, ValueError):
                unknown.add(identity)
                continue
            sums[identity] = sums.get(identity, 0) + int(numeric)

    def total(key, day, field):
        return None if (key, day, field) in unknown else sums.get((key, day, field), 0)

    sheet = book[title]
    rows = iter(sheet.iter_rows(values_only=True))
    actual_headers = list(next(rows, ()))
    expected_headers = ["设备号", "记录数", *(headers[i] for i, _, _ in metrics)]
    mismatches, actual_devices = [], []
    for row_number, values in enumerate(rows, start=2):
        key = values[0]
        actual_devices.append(key)
        if key not in sizes or len(values) != len(expected_headers):
            mismatches.append({"row": row_number, "reason": "设备或列数不匹配"})
            continue
        expected = [key, sizes[key]]
        for _, day, field in metrics:
            if field == "回复率":
                sends, replies = total(key, day, "发送窗口数"), total(key, day, "回复窗口数")
                value = replies / sends if sends and replies is not None else None
            else:
                value = total(key, day, field)
            expected.append(value)
        for column, (observed, wanted) in enumerate(zip(values, expected), start=1):
            if wanted is None:
                equal = observed is None or observed == ""
            elif isinstance(wanted, float):
                equal = isinstance(observed, (int, float)) and not isinstance(observed, bool) and abs(observed - wanted) <= 1e-12
            elif isinstance(wanted, int):
                equal = isinstance(observed, (int, float)) and not isinstance(observed, bool) and observed == wanted
            else:
                equal = observed == wanted
            if not equal:
                mismatches.append({"row": row_number, "column": column, "expected": wanted, "actual": observed})
    expected_devices = sorted(sizes, key=lambda key: (2, "") if key == "未识别设备" else
                              (0, int(key)) if key.isdigit() else (1, key))
    return {"matches": schema_ok and actual_headers == expected_headers and not mismatches and actual_devices == expected_devices,
            "schema_matches": schema_ok and actual_headers == expected_headers,
            "device_order_matches": actual_devices == expected_devices,
            "source_rows": source_count, "device_count": len(sizes) - int("未识别设备" in sizes),
            "date_count": len(dates), "unrecognized_device_rows": sizes.get("未识别设备", 0),
            "mismatch_count": len(mismatches), "mismatches": mismatches[:25],
            "method": "independent_decimal_row_major_from_source_not_generation_receipt"}
