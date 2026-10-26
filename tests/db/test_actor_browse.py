"""演员浏览: ActorBrowseParams 校验 + browse_actors SQL. HTTP 接线见 tests/api/test_actors.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from amane.db.models import ActorSortField, FacetKind, SortOrder
from amane.db.repo_types import ActorBrowseParams
from amane.enums import ActorGender

if TYPE_CHECKING:
    from amane.db.repository import Repository


async def _seed_filter_actors(repo: Repository) -> dict[str, int]:
    """准备字段筛选用例演员, 返回 name→id."""
    await repo.upsert_metadata(
        number="ACT-FLT-1", actors=["FltTall", "FltShort", "FltNullMetric", "FltAliasOnly", "FltMale"]
    )
    actors, _ = await repo.list_facets(FacetKind.ACTOR)
    by_name = {a.name: a.id for a in actors if a.id is not None and a.name.startswith("Flt")}

    tall = await repo.get_actor(by_name["FltTall"])
    assert tall is not None
    tall.gender = ActorGender.FEMALE
    tall.birthday = "1990-06-15"
    tall.height = 168
    tall.bust = 86
    tall.waist = 58
    tall.hip = 88
    tall.cup = "D"
    tall.birthplace = "Tokyo"
    tall.image_urls = ["https://img.example/tall.jpg"]
    await repo.save_actor(tall, aliases=["高子"])

    short = await repo.get_actor(by_name["FltShort"])
    assert short is not None
    short.gender = ActorGender.FEMALE
    short.birthday = "1995-01-01"
    short.height = 150
    short.bust = 80
    short.waist = 55
    short.hip = 82
    short.cup = "B"
    short.birthplace = "Osaka"
    await repo.save_actor(short)

    null_metric = await repo.get_actor(by_name["FltNullMetric"])
    assert null_metric is not None
    null_metric.gender = ActorGender.FEMALE
    await repo.save_actor(null_metric)

    alias_only = await repo.get_actor(by_name["FltAliasOnly"])
    assert alias_only is not None
    await repo.save_actor(alias_only, aliases=["HiddenAliasXYZ"])

    male = await repo.get_actor(by_name["FltMale"])
    assert male is not None
    male.gender = ActorGender.MALE
    male.height = 175
    male.birthday = "1988-12-01"
    await repo.save_actor(male)

    return {k: v for k, v in by_name.items() if v is not None}


_FILTER_CASES: list[tuple[ActorBrowseParams, set[str]]] = [
    (ActorBrowseParams(gender=[ActorGender.FEMALE], limit=200), {"FltTall", "FltShort", "FltNullMetric"}),
    (
        ActorBrowseParams(gender=[ActorGender.FEMALE, ActorGender.MALE], limit=200),
        {"FltTall", "FltShort", "FltNullMetric", "FltMale"},
    ),
    (ActorBrowseParams(height_min=160, limit=200), {"FltTall", "FltMale"}),
    (ActorBrowseParams(height_max=155, limit=200), {"FltShort"}),
    (ActorBrowseParams(height_min=155, height_max=170, limit=200), {"FltTall"}),
    (ActorBrowseParams(birthday_min="1992-01-01", limit=200), {"FltShort"}),
    (ActorBrowseParams(birthday_max="1991-12-31", limit=200), {"FltTall", "FltMale"}),
    (ActorBrowseParams(birthday_min="1990-01-01", birthday_max="1990-12-31", limit=200), {"FltTall"}),
    (ActorBrowseParams(cup_min="C", cup_max="E", limit=200), {"FltTall"}),
    (ActorBrowseParams(cup_max="B", limit=200), {"FltShort"}),
    (ActorBrowseParams(bust_min=85, limit=200), {"FltTall"}),
    (ActorBrowseParams(birthplace="kyo", limit=200), {"FltTall"}),
    (ActorBrowseParams(height_min=160, has_image=True, limit=200), {"FltTall"}),
]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"height_min": 200, "height_max": 150},
        {"birthday_min": "1995-01-01", "birthday_max": "1990-01-01"},
        {"cup_min": "E", "cup_max": "A"},
        {"birthday_min": "not-a-date"},
        {"birthday_max": "1990/13/40"},
    ],
    ids=["height", "birthday_range", "cup", "birthday_min_fmt", "birthday_max_fmt"],
)
def test_browse_params_reject_invalid(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ActorBrowseParams.model_validate(kwargs)


@pytest.mark.asyncio(loop_scope="function")
async def test_browse_actors_field_filters(repo: Repository) -> None:
    ids = await _seed_filter_actors(repo)

    alias_items, _ = await repo.browse_actors(ActorBrowseParams(search="HiddenAliasXYZ", limit=200))
    found = {i.id for i in alias_items}
    assert ids["FltAliasOnly"] in found
    assert ids["FltTall"] not in found

    for params, expect_names in _FILTER_CASES:
        items, _ = await repo.browse_actors(params)
        got = {i.name for i in items if i.name.startswith("Flt")}
        assert got == expect_names, params
        if params.height_min is not None or params.height_max is not None:
            assert ids["FltNullMetric"] not in {i.id for i in items}


@pytest.mark.asyncio(loop_scope="function")
async def test_browse_actors_has_person_and_sort(repo: Repository) -> None:
    await repo.upsert_metadata(number="ACT-BR-1", actors=["EmptyOne", "FilledOne"])
    actors, _ = await repo.list_facets(FacetKind.ACTOR)
    empty_id = next(a.id for a in actors if a.name == "EmptyOne")
    filled_id = next(a.id for a in actors if a.name == "FilledOne")
    assert empty_id is not None and filled_id is not None

    filled = await repo.get_actor(filled_id)
    assert filled is not None
    filled.birthday = "1991-01-01"
    filled.height = 160
    filled.image_urls = ["https://img.example/a.jpg"]
    await repo.save_actor(filled)

    no_person, _ = await repo.browse_actors(ActorBrowseParams(has_person=False, limit=200))
    assert all(i.name != "FilledOne" or not i.birthday for i in no_person)
    assert any(i.id == empty_id for i in no_person)

    with_image, _ = await repo.browse_actors(ActorBrowseParams(has_image=True, limit=200))
    assert any(i.id == filled_id for i in with_image)
    assert all(i.image_urls for i in with_image)

    by_bday, _ = await repo.browse_actors(
        ActorBrowseParams(sort_by=ActorSortField.BIRTHDAY, order=SortOrder.ASC, limit=100)
    )
    bdays = [i.birthday for i in by_bday if i.birthday]
    assert bdays == sorted(bdays)


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (SortOrder.ASC, [ActorGender.FEMALE, ActorGender.MALE, ActorGender.UNKNOWN]),
        (SortOrder.DESC, [ActorGender.MALE, ActorGender.FEMALE, ActorGender.UNKNOWN]),
    ],
    ids=["asc", "desc"],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_browse_actors_sort_by_gender(repo: Repository, order: SortOrder, expected: list[ActorGender]) -> None:
    """性别排序: 升降序只翻转已知性别, 未标注的恒在末尾 (按字母序时 DESC 会把它顶到最前)."""
    await _seed_filter_actors(repo)
    items, _ = await repo.browse_actors(ActorBrowseParams(sort_by=ActorSortField.GENDER, order=order, limit=200))
    got = [i.gender for i in items]
    # 同性别内部次序由副排序决定, 这里只断言分组顺序: 折叠相邻重复后的序列即期望顺序.
    assert [g for i, g in enumerate(got) if i == 0 or got[i - 1] != g] == expected
