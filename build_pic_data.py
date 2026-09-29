#!/usr/bin/env python3
"""Build a five-year PIC dataset from ONS time-series JSON pages.

For every category/month the output stores:
- annualRate: annual inflation rate, using that year's weights for aggregates
- indexDynamicWeights: component indices aggregated using that year's weights
- indexFixedWeights: component indices aggregated using one latest common weight year
- index: an alias selected with --index-weight-mode for easy front-end use
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import requests

PAGE_URL = "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/{code}/data"
CDID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{3}$")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
)}
IndexWeightMode = Literal["dynamic", "fixed"]


@dataclass(frozen=True)
class Component:
    annual_rate_code: str
    index_code: str
    weight_code: str | None = None


@dataclass(frozen=True)
class Category:
    category_id: str
    name: str
    group_id: str
    group_name: str
    aggregation: Literal["single", "weighted"]
    components: tuple[Component, ...]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def is_cdid(value: Any) -> bool:
    return bool(CDID_RE.fullmatch(clean(value)))


def load_categories(config_path: Path) -> list[Category]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {config_path}: {exc}") from exc

    categories: list[Category] = []
    seen_ids: set[str] = set()

    for group in payload.get("groups", []):
        group_id = clean(group.get("id"))
        group_name = clean(group.get("name"))
        if not group_id or not group_name:
            raise ValueError("Every group must have a non-empty id and name")

        for item in group.get("categories", []):
            category_id = clean(item.get("id"))
            name = clean(item.get("name"))
            aggregation = clean(item.get("aggregation")).lower()
            if not category_id or not name:
                raise ValueError("Every category must have a non-empty id and name")
            if category_id in seen_ids:
                raise ValueError(f"Duplicate category id: {category_id}")
            if aggregation not in {"single", "weighted"}:
                raise ValueError(
                    f"Category {category_id} must have aggregation 'single' or 'weighted'"
                )

            components: list[Component] = []
            for series in item.get("series", []):
                rate_code = clean(series.get("annualRateCode")).upper()
                index_code = clean(series.get("indexCode")).upper()
                weight_code = clean(series.get("weightCode")).upper() or None

                if not is_cdid(rate_code):
                    raise ValueError(
                        f"Invalid annualRateCode {rate_code!r} in category {category_id}"
                    )
                if not is_cdid(index_code):
                    raise ValueError(
                        f"Invalid indexCode {index_code!r} in category {category_id}"
                    )
                if weight_code and not is_cdid(weight_code):
                    raise ValueError(
                        f"Invalid weightCode {weight_code!r} in category {category_id}"
                    )
                components.append(Component(rate_code, index_code, weight_code))

            if not components:
                raise ValueError(f"Category {category_id} has no series")
            if aggregation == "single" and len(components) != 1:
                raise ValueError(
                    f"Single category {category_id} must contain exactly one component"
                )
            if aggregation == "weighted":
                if len(components) < 2:
                    raise ValueError(
                        f"Weighted category {category_id} must contain at least two components"
                    )
                if any(not component.weight_code for component in components):
                    raise ValueError(
                        f"Every component in weighted category {category_id} needs a weightCode"
                    )

            categories.append(Category(
                category_id=category_id,
                name=name,
                group_id=group_id,
                group_name=group_name,
                aggregation=aggregation,  # type: ignore[arg-type]
                components=tuple(components),
            ))
            seen_ids.add(category_id)

    if not categories:
        raise ValueError("No categories were found in the configuration")
    return categories


class OnsClient:
    def __init__(
        self,
        timeout: float = 30.0,
        attempts: int = 4,
        verify: bool | str = True,
        request_delay: float = 0.15,
    ):
        self.timeout = timeout
        self.attempts = attempts
        self.verify = False
        self.request_delay = request_delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PIC-inflation-data-builder/4.0",
            "Accept": "application/json",
        })
        self.cache: dict[str, dict[str, Any]] = {}

    def get(self, code: str) -> dict[str, Any]:
        code = code.upper()
        if code in self.cache:
            return self.cache[code]

        url = PAGE_URL.format(code=code)
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    verify=self.verify,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("top-level JSON value is not an object")
                self.cache[code] = payload
                if self.request_delay:
                    time.sleep(self.request_delay)
                return payload
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.attempts:
                    raise RuntimeError(f"Could not retrieve ONS JSON {url}: {exc}") from exc
                wait = 0.75 * (2 ** (attempt - 1))
                logging.warning(
                    "Request for %s failed on attempt %d/%d; retrying in %.2fs",
                    code, attempt, self.attempts, wait,
                )
                time.sleep(wait)

        raise AssertionError("unreachable")


def number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in {"", "..", "-", "na", "n/a", "null"}:
        return None
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def parse_month(item: dict[str, Any]) -> str | None:
    year_text = clean(item.get("year"))
    month_text = clean(item.get("month")).upper()[:3]
    if year_text.isdigit() and month_text in MONTHS:
        return f"{int(year_text):04d}-{MONTHS[month_text]:02d}-01"

    date_text = re.sub(r"\s+", " ", clean(item.get("date"))).upper()
    patterns = [
        r"^((?:19|20)\d{2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)$",
        r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+((?:19|20)\d{2})$",
    ]
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, date_text)
        if match:
            year, month = (
                (match.group(1), match.group(2))
                if index == 0 else (match.group(2), match.group(1))
            )
            return f"{int(year):04d}-{MONTHS[month]:02d}-01"
    return None


def monthly_values(payload: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in payload.get("months", []):
        if not isinstance(item, dict):
            continue
        date = parse_month(item)
        value = number(item.get("value"))
        if date is not None and value is not None:
            result[date] = value
    if not result:
        raise ValueError("ONS JSON contained no recognised monthly observations")
    return result


def annual_weights(payload: dict[str, Any]) -> dict[int, float]:
    result: dict[int, float] = {}
    for item in payload.get("years", []):
        if not isinstance(item, dict):
            continue
        year_text = clean(item.get("year") or item.get("date"))
        match = re.search(r"\b(?:19|20)\d{2}\b", year_text)
        value = number(item.get("value"))
        if match and value is not None:
            result[int(match.group(0))] = value
    if not result:
        raise ValueError("ONS JSON contained no recognised annual observations")
    return result


def subtract_years(date: str, years: int) -> str:
    year, month, day = map(int, date.split("-"))
    return f"{year - years:04d}-{month:02d}-{day:02d}"


def weighted_average(values: list[float], weights: list[float]) -> float:
    if len(values) != len(weights) or not values:
        raise ValueError("Values and weights must be non-empty and the same length")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("Total weight must be greater than zero")
    return sum(value * weight for value, weight in zip(values, weights)) / total_weight


def latest_common_weight_year(weights_by_code: dict[str, dict[int, float]]) -> int:
    common_years = set.intersection(
        *(set(year_values) for year_values in weights_by_code.values())
    )
    if not common_years:
        raise ValueError("Component weight series have no common year")
    return max(common_years)


def build_category(
    category: Category,
    client: OnsClient,
    index_weight_mode: IndexWeightMode,
) -> dict[str, Any]:
    rates_by_code = {
        component.annual_rate_code: monthly_values(client.get(component.annual_rate_code))
        for component in category.components
    }
    indexes_by_code = {
        component.index_code: monthly_values(client.get(component.index_code))
        for component in category.components
    }

    all_monthly_series = [*rates_by_code.values(), *indexes_by_code.values()]
    common_dates = set.intersection(*(set(series) for series in all_monthly_series))
    if not common_dates:
        raise ValueError(
            f"{category.category_id} {category.name}: no dates shared by all rate and index series"
        )

    end = max(common_dates)
    start = subtract_years(end, 5)
    dates = sorted(date for date in common_dates if start <= date <= end)

    weights_by_code: dict[str, dict[int, float]] = {}
    fixed_weight_year: int | None = None
    fixed_weights: dict[str, float] = {}

    if category.aggregation == "weighted":
        weights_by_code = {
            component.weight_code: annual_weights(client.get(component.weight_code))
            for component in category.components
            if component.weight_code is not None
        }
        fixed_weight_year = latest_common_weight_year(weights_by_code)
        fixed_weights = {
            weight_code: year_values[fixed_weight_year]
            for weight_code, year_values in weights_by_code.items()
        }

    observations: list[dict[str, Any]] = []
    for date in dates:
        if category.aggregation == "single":
            component = category.components[0]
            annual_rate = rates_by_code[component.annual_rate_code][date]
            dynamic_index = indexes_by_code[component.index_code][date]
            fixed_index = dynamic_index
        else:
            year = int(date[:4])
            component_rates: list[float] = []
            component_indexes: list[float] = []
            dynamic_weights: list[float] = []
            fixed_weight_values: list[float] = []
            missing: list[str] = []

            for component in category.components:
                rate = rates_by_code[component.annual_rate_code].get(date)
                index_value = indexes_by_code[component.index_code].get(date)
                dynamic_weight = weights_by_code[component.weight_code].get(year)  # type: ignore[index]
                fixed_weight = fixed_weights.get(component.weight_code)  # type: ignore[arg-type]

                if (
                    rate is None
                    or index_value is None
                    or dynamic_weight is None
                    or fixed_weight is None
                ):
                    missing.append(component.annual_rate_code)
                    continue

                component_rates.append(rate)
                component_indexes.append(index_value)
                dynamic_weights.append(dynamic_weight)
                fixed_weight_values.append(fixed_weight)

            if missing:
                logging.warning(
                    "Skipping %s for %s because values/weights are incomplete: %s",
                    date, category.name, ", ".join(missing),
                )
                continue

            # Annual rates retain the existing PIC method: that year's weights.
            annual_rate = weighted_average(component_rates, dynamic_weights)
            dynamic_index = weighted_average(component_indexes, dynamic_weights)
            fixed_index = weighted_average(component_indexes, fixed_weight_values)

        selected_index = dynamic_index if index_weight_mode == "dynamic" else fixed_index
        observations.append({
            "date": date,
            "annualRate": round(annual_rate, 6),
            "index": round(selected_index, 6),
            "indexDynamicWeights": round(dynamic_index, 6),
            "indexFixedWeights": round(fixed_index, 6),
        })

    if not observations:
        raise ValueError(
            f"{category.category_id} {category.name}: no complete observations produced"
        )

    result: dict[str, Any] = {
        "id": category.category_id,
        "name": category.name,
        "groupId": category.group_id,
        "groupName": category.group_name,
        "aggregation": category.aggregation,
        "periodStart": observations[0]["date"],
        "periodEnd": observations[-1]["date"],
        "components": [
            {
                "annualRateCode": component.annual_rate_code,
                "indexCode": component.index_code,
                **(
                    {"weightCode": component.weight_code}
                    if component.weight_code else {}
                ),
            }
            for component in category.components
        ],
        "data": observations,
    }
    if fixed_weight_year is not None:
        result["fixedWeightReferenceYear"] = fixed_weight_year
    return result


def has_new_month(client: OnsClient, categories: list[Category], output_path: Path) -> bool:
    if not output_path.exists():
        logging.info("No existing output found; proceeding with the initial build")
        return True

    try:
        existing = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not inspect existing output %s: %s; proceeding", output_path, exc)
        return True

    if not isinstance(existing, dict):
        logging.warning("Existing output %s has an invalid format; proceeding", output_path)
        return True

    existing_categories = existing.get("categories", [])
    dated_categories = [
        category for category in existing_categories
        if isinstance(category, dict) and isinstance(category.get("periodEnd"), str)
    ] if isinstance(existing_categories, list) else []
    latest_output_category = max(
        dated_categories, key=lambda category: category["periodEnd"], default=None
    )
    metadata = existing.get("metadata")
    metadata_end = metadata.get("periodEnd") if isinstance(metadata, dict) else None
    previous_end = (
        latest_output_category["periodEnd"]
        if latest_output_category else metadata_end
    )
    if (
        not isinstance(previous_end, str)
        or not re.fullmatch(r"\d{4}-\d{2}-01", previous_end)
        or not categories
    ):
        logging.warning("Could not determine the existing data's latest month; proceeding")
        return True

    probe_category = next(
        (
            category for category in categories
            if latest_output_category
            and category.category_id == latest_output_category.get("id")
        ),
        categories[0],
    )
    component = probe_category.components[0]
    rate_dates = monthly_values(client.get(component.annual_rate_code))
    index_dates = monthly_values(client.get(component.index_code))
    common_dates = set(rate_dates) & set(index_dates)
    if not common_dates:
        raise ValueError(f"No shared monthly observations for {probe_category.name}")

    latest_source_month = max(common_dates)
    if latest_source_month <= previous_end:
        logging.info(
            "No new month available from ONS (latest: %s; existing: %s)",
            latest_source_month, previous_end,
        )
        return False

    logging.info("New ONS month available: %s (existing: %s)", latest_source_month, previous_end)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to categories_v3.json")
    parser.add_argument("output", type=Path, help="Path for the generated JSON file")
    parser.add_argument(
        "--index-weight-mode",
        choices=("dynamic", "fixed"),
        default="dynamic",
        help=(
            "Controls the value copied into each observation's 'index' field. "
            "Both index variants are always stored. Default: dynamic"
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--request-delay", type=float, default=0.15)
    parser.add_argument("--indent", type=int, default=2)
    ssl_group = parser.add_mutually_exclusive_group()
    ssl_group.add_argument(
        "--ca-bundle",
        type=Path,
        help="Path to a PEM CA bundle, such as an organisation root certificate bundle",
    )
    ssl_group.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification. Use only as a temporary workaround.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    categories = load_categories(args.config)
    logging.info("Found %d categories", len(categories))
    logging.info("Selected index weight mode: %s", args.index_weight_mode)

    verify: bool | str = True
    if args.insecure:
        verify = False
        logging.warning("TLS certificate verification is disabled")
    elif args.ca_bundle:
        verify = str(args.ca_bundle)

    client = OnsClient(
        timeout=args.timeout,
        attempts=args.attempts,
        verify=verify,
        request_delay=args.request_delay,
    )

    if not has_new_month(client, categories, args.output):
        return

    output_categories = []
    for index, category in enumerate(categories, 1):
        logging.info(
            "[%d/%d] %s %s",
            index, len(categories), category.category_id, category.name,
        )
        output_categories.append(
            build_category(category, client, args.index_weight_mode)
        )

    all_dates = [
        point["date"]
        for category in output_categories
        for point in category["data"]
    ]
    fixed_years = sorted({
        category["fixedWeightReferenceYear"]
        for category in output_categories
        if "fixedWeightReferenceYear" in category
    })

    result = {
        "metadata": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "Office for National Statistics time-series JSON pages",
            "periodStart": min(all_dates),
            "periodEnd": max(all_dates),
            "categoryCount": len(output_categories),
            "selectedIndexWeightMode": args.index_weight_mode,
            "indexFields": {
                "index": f"Selected {args.index_weight_mode}-weight index",
                "indexDynamicWeights": "Index using each observation year's weights",
                "indexFixedWeights": "Index using the latest common weight year for the category",
            },
            "fixedWeightReferenceYears": fixed_years,
        },
        "categories": output_categories,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=args.indent) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    logging.info("Wrote %s", args.output)


if __name__ == "__main__":
    main()
