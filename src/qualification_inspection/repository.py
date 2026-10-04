"""JSON 文件仓储：集合持久化与按 ID 索引。

默认无持久化（内存仓储）；``JsonRepository`` 把全部集合写入单个 JSON 文件，
写入采用临时文件原子替换，保证崩溃不产生半截文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Generic, TypeVar

from .models import (
    Authorization,
    Entity,
    InspectionRun,
    Person,
    Qualification,
    RenewalCase,
    Restriction,
    RiskItem,
    ServiceRecord,
    Suspension,
    TemporaryCredential,
)

T = TypeVar("T", bound=Entity)


class Collection(Generic[T]):
    def __init__(self, owner: "Repository", name: str, cls: type[T],
                 key_attr: str) -> None:
        self._owner = owner
        self._name = name
        self._cls = cls
        self._key_attr = key_attr
        self._items: dict[str, T] = {}

    # 仓储加载/转储 ──────────────────────────────────────────────────────
    def _load(self, rows: list[dict]) -> None:
        self._items = {str(row[self._key_attr]): self._cls.from_dict(row) for row in rows}

    def _dump(self) -> list[dict]:
        return [item.to_dict() for item in self._items.values()]

    # 查询 ──────────────────────────────────────────────────────────────
    def get(self, key: str) -> T | None:
        return self._items.get(key)

    def require(self, key: str) -> T:
        item = self._items.get(key)
        if item is None:
            raise KeyError(f"{self._name} 不存在: {key}")
        return item

    def all(self) -> list[T]:
        return list(self._items.values())

    def filter(self, **criteria: object) -> list[T]:
        result = []
        for item in self._items.values():
            if all(getattr(item, k, None) == v for k, v in criteria.items()):
                result.append(item)
        return result

    # 写入 ──────────────────────────────────────────────────────────────
    def put(self, item: T) -> T:
        self._items[str(getattr(item, self._key_attr))] = item
        self._owner._mark_dirty()
        return item

    def delete(self, key: str) -> None:
        if key in self._items:
            del self._items[key]
            self._owner._mark_dirty()

    def clear(self) -> None:
        if self._items:
            self._items.clear()
            self._owner._mark_dirty()


class Repository:
    """仓储门面，持有全部集合并负责整体持久化。"""

    def __init__(self) -> None:
        self.persons = Collection[Person](self, "persons", Person, "person_id")
        self.qualifications = Collection[Qualification](
            self, "qualifications", Qualification, "qual_id")
        self.authorizations = Collection[Authorization](
            self, "authorizations", Authorization, "authorization_id")
        self.renewal_cases = Collection[RenewalCase](
            self, "renewal_cases", RenewalCase, "case_id")
        self.credentials = Collection[TemporaryCredential](
            self, "credentials", TemporaryCredential, "cred_id")
        self.suspensions = Collection[Suspension](
            self, "suspensions", Suspension, "order_no")
        self.restrictions = Collection[Restriction](
            self, "restrictions", Restriction, "restriction_id")
        self.risks = Collection[RiskItem](self, "risks", RiskItem, "risk_id")
        self.service_records = Collection[ServiceRecord](
            self, "service_records", ServiceRecord, "record_id")
        self.runs = Collection[InspectionRun](self, "runs", InspectionRun, "period")
        self._collections = {
            "persons": self.persons,
            "qualifications": self.qualifications,
            "authorizations": self.authorizations,
            "renewal_cases": self.renewal_cases,
            "credentials": self.credentials,
            "suspensions": self.suspensions,
            "restrictions": self.restrictions,
            "risks": self.risks,
            "service_records": self.service_records,
            "runs": self.runs,
        }
        self._dirty = False

    def _mark_dirty(self) -> None:
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    def snapshot(self) -> dict[str, list[dict]]:
        return {name: coll._dump() for name, coll in self._collections.items()}

    def load_dict(self, data: dict[str, list[dict]]) -> None:
        for name, coll in self._collections.items():
            coll._load(data.get(name, []))
        self._dirty = False


class JsonRepository(Repository):
    """JSON 文件仓储。

    autoload=True 时构造即载入已有文件；写操作后需显式调用 :meth:`save`，
    服务层在每次命令成功结束时统一保存。
    """

    def __init__(self, path: str | Path, autoload: bool = True) -> None:
        super().__init__()
        self.path = Path(path)
        if autoload and self.path.exists():
            self.load_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self) -> None:
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".repo-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._dirty = False
