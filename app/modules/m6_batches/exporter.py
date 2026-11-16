import csv
import io

EXPORT_FIELDS = ["flight_key", "departure_utc", "arrival_utc", "status", "reason_code"]
# 导出字段白名单：external_id 不在其中，旧导出口径也无法带出外部行程号
SENSITIVE_FIELDS = ("external_id",)


def itineraries_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: sanitized(row).get(field, "") for field in EXPORT_FIELDS})
    return output.getvalue()


def sanitized(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in SENSITIVE_FIELDS}
