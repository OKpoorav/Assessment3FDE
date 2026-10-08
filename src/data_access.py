"""Typed, read-only access to the synthetic business data.

The starter version returned pandas DataFrames, which silently turned blank CSV
cells (e.g. a vendor with no review date, the VP with no manager) into float NaN.
NaN is truthy, is not JSON-serialisable and compares unequal to itself, which is
exactly the kind of value that leaks into an LLM prompt or a date check and
produces a wrong answer. This module uses the csv module instead and normalises
blank cells to ``None``.

``DataStore`` also accepts overrides so evaluation scenarios can add requests or
patch registry rows without editing the shared data files.
"""
from __future__ import annotations

import copy
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import DATA_DIR, POLICY_PATH

Row = dict[str, Any]

_INT_FIELDS = {
    "annual_software_budget_usd",
    "committed_usd",
    "available_usd",
    "annual_cost_usd",
    "licensed_seats",
    "annual_amount_usd",
}


def _clean(row: dict[str, str]) -> Row:
    out: Row = {}
    for key, value in row.items():
        value = (value or "").strip()
        if value == "":
            out[key] = None
        elif key in _INT_FIELDS:
            out[key] = float(value) if "." in value else int(value)
        else:
            out[key] = value
    return out


def _read_csv(path: Path) -> list[Row]:
    with path.open(encoding="utf-8", newline="") as f:
        return [_clean(r) for r in csv.DictReader(f)]


def norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


@dataclass
class DataStore:
    employees: list[Row]
    budgets: list[Row]
    catalog: list[Row]
    vendors: list[Row]
    purchases: list[Row]
    requests: list[Row]
    policy_text: str
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> "DataStore":
        return cls(
            employees=_read_csv(data_dir / "employees.csv"),
            budgets=_read_csv(data_dir / "department_budgets.csv"),
            catalog=_read_csv(data_dir / "software_catalog.csv"),
            vendors=_read_csv(data_dir / "vendors.csv"),
            purchases=_read_csv(data_dir / "purchase_history.csv"),
            requests=json.loads((data_dir / "requests.json").read_text(encoding="utf-8")),
            policy_text=(data_dir / POLICY_PATH.name).read_text(encoding="utf-8"),
        )

    def with_overrides(self, overrides: dict[str, Any] | None) -> "DataStore":
        """Return a copy with rows upserted by primary key.

        ``overrides`` may contain ``vendors``, ``employees``, ``budgets``, ``catalog``
        or ``requests`` lists. Rows replace existing rows with the same key.
        """
        if not overrides:
            return self
        clone = copy.deepcopy(self)
        keys = {
            "vendors": "vendor_name",
            "employees": "employee_id",
            "budgets": "department",
            "catalog": "software_id",
            "requests": "request_id",
        }
        for table, key in keys.items():
            for row in overrides.get(table, []):
                rows: list[Row] = getattr(clone, table)
                rows[:] = [r for r in rows if r.get(key) != row.get(key)] + [row]
        return clone

    # Lookups -----------------------------------------------------------------

    def get_request(self, request_id: str) -> Row:
        for request in self.requests:
            if request.get("request_id") == request_id:
                return request
        raise KeyError(f"Unknown request_id: {request_id}")

    def get_employee(self, employee_id: str | None) -> Row | None:
        return next((e for e in self.employees if e["employee_id"] == employee_id), None)

    def get_budget(self, department: str | None) -> Row | None:
        return next((b for b in self.budgets if norm(b["department"]) == norm(department)), None)

    def get_vendor(self, vendor_name: str | None) -> Row | None:
        return next((v for v in self.vendors if norm(v["vendor_name"]) == norm(vendor_name)), None)


_DEFAULT_STORE: DataStore | None = None


def default_store() -> DataStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = DataStore.load()
    return _DEFAULT_STORE


# Backwards-compatible helpers used by the starter UI / scripts.

def load_requests() -> list[Row]:
    return default_store().requests


def get_request(request_id: str) -> Row:
    return default_store().get_request(request_id)


def load_policy_text() -> str:
    return default_store().policy_text
