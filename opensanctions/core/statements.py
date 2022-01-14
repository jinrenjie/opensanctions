import structlog
from hashlib import sha1
from datetime import datetime
from typing import AsyncGenerator, List, Optional, TypedDict, cast
from sqlalchemy.future import select
from sqlalchemy.sql.expression import delete, update, insert, Select
from sqlalchemy.sql.functions import func
from nomenklatura import Resolver
from followthemoney import model
from followthemoney.types import registry

from opensanctions import settings
from opensanctions.core.db import stmt_table, canonical_table
from opensanctions.core.db import Conn, upsert_func
from opensanctions.core.dataset import Dataset
from opensanctions.core.entity import Entity

log = structlog.get_logger(__name__)
BASE = "id"


class Statement(TypedDict):
    """A single statement about a property relevant to an entity.

    For example, this could be useddocker to say: "In dataset A, entity X has the
    property `name` set to 'John Smith'. I first observed this at K, and last
    saw it at L."

    Null property values are not supported. This might need to change if we
    want to support making property-less entities.
    """

    id: str
    entity_id: str
    canonical_id: str
    prop: str
    prop_type: str
    schema: str
    value: str
    dataset: str
    target: bool
    unique: bool
    first_seen: datetime
    last_seen: datetime


def stmt_key(dataset, entity_id, prop, value):
    """Hash the key properties of a statement record to make a unique ID."""
    key = f"{dataset}.{entity_id}.{prop}.{value}"
    return sha1(key.encode("utf-8")).hexdigest()


def statements_from_entity(
    entity: Entity, dataset: Dataset, unique: bool = False
) -> List[Statement]:
    if entity.id is None:
        return []
    values: List[Statement] = [
        {
            "id": stmt_key(dataset.name, entity.id, BASE, entity.id),
            "entity_id": entity.id,
            "canonical_id": entity.id,
            "prop": BASE,
            "prop_type": BASE,
            "schema": entity.schema.name,
            "value": entity.id,
            "dataset": dataset.name,
            "target": entity.target,
            "unique": unique,
            "first_seen": settings.RUN_TIME,
            "last_seen": settings.RUN_TIME,
        }
    ]
    for prop, value in entity.itervalues():
        stmt: Statement = {
            "id": stmt_key(dataset.name, entity.id, prop.name, value),
            "entity_id": entity.id,
            "canonical_id": entity.id,
            "prop": prop.name,
            "prop_type": prop.type.name,
            "schema": entity.schema.name,
            "value": value,
            "dataset": dataset.name,
            "target": entity.target,
            "unique": unique,
            "first_seen": settings.RUN_TIME,
            "last_seen": settings.RUN_TIME,
        }
        values.append(stmt)
    return values


async def save_statements(conn: Conn, values: List[Statement]) -> None:
    if not len(values):
        return None

    istmt = upsert_func(stmt_table).values(values)
    stmt = istmt.on_conflict_do_update(
        index_elements=["id"],
        set_=dict(
            canonical_id=istmt.excluded.canonical_id,
            schema=istmt.excluded.schema,
            prop_type=istmt.excluded.prop_type,
            target=istmt.excluded.target,
            unique=istmt.excluded.unique,
            last_seen=istmt.excluded.last_seen,
        ),
    )
    await conn.execute(stmt)
    return None


async def all_statements(
    conn: Conn, dataset=None, canonical_id=None, inverted_ids=None
) -> AsyncGenerator[Statement, None]:
    q = select(stmt_table)
    if canonical_id is not None:
        q = q.filter(stmt_table.c.canonical_id == canonical_id)
    if inverted_ids is not None:
        alias = stmt_table.alias()
        sq = select(func.distinct(alias.c.canonical_id))
        sq = sq.filter(alias.c.prop_type == registry.entity.name)
        sq = sq.filter(alias.c.value.in_(inverted_ids))
        # sq = sq.subquery()
        # cte = select(func.distinct(stmt_table.c.canonical_id).label("canonical_id"))
        # cte = cte.where(stmt_table.c.prop_type == registry.entity.name)
        # cte = cte.where(stmt_table.c.value.in_(inverted_ids))
        # cte = cte.cte("inverted")
        # Find entities which refer to the given entity in one of their
        # property values.
        # inverted = aliased(cls)
        q = q.filter(stmt_table.c.canonical_id.in_(sq))
        # q = q.filter(inverted.prop_type == registry.entity.name)
        # q = q.filter(inverted.value.in_(inverted_ids))
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    q = q.order_by(stmt_table.c.canonical_id.asc())
    result = await conn.stream(q)
    async for row in result:
        yield cast(Statement, row._asdict())


def filtered_statements_query(
    q: Select,
    dataset: Optional[Dataset] = None,
    entity_id: Optional[str] = None,
    canonical_id: Optional[str] = None,
    prop: Optional[str] = None,
    value: Optional[str] = None,
    schema: Optional[str] = None,
) -> Select:

    if canonical_id is not None:
        q = q.filter(stmt_table.c.canonical_id == canonical_id)
    if entity_id is not None:
        q = q.filter(stmt_table.c.entity_id == entity_id)
    if prop is not None:
        q = q.filter(stmt_table.c.prop == prop)
    if value is not None:
        q = q.filter(stmt_table.c.value == value)
    if schema is not None:
        q = q.filter(stmt_table.c.schema == schema)
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    return q


async def count_statements(
    conn: Conn,
    dataset: Optional[Dataset] = None,
    entity_id: Optional[str] = None,
    canonical_id: Optional[str] = None,
    prop: Optional[str] = None,
    value: Optional[str] = None,
    schema: Optional[str] = None,
) -> int:
    q = select(func.count(stmt_table.c.id))
    q = filtered_statements_query(
        q,
        dataset=dataset,
        entity_id=entity_id,
        canonical_id=canonical_id,
        prop=prop,
        value=value,
        schema=schema,
    )
    return await conn.scalar(q)


async def paged_statements(
    conn: Conn,
    limit: int = 100,
    offset: int = 0,
    dataset: Optional[Dataset] = None,
    entity_id: Optional[str] = None,
    canonical_id: Optional[str] = None,
    prop: Optional[str] = None,
    value: Optional[str] = None,
    schema: Optional[str] = None,
) -> AsyncGenerator[Statement, None]:
    # Make the order of entities stable.
    q = select(stmt_table)
    q = filtered_statements_query(
        q,
        dataset=dataset,
        entity_id=entity_id,
        canonical_id=canonical_id,
        prop=prop,
        value=value,
        schema=schema,
    )
    q = q.order_by(stmt_table.c.canonical_id.desc())
    q = q.order_by(stmt_table.c.entity_id.desc())
    q = q.order_by(stmt_table.c.prop.desc())

    q = q.offset(offset).limit(limit)
    result = await conn.stream(q)
    async for row in result:
        yield cast(Statement, row._asdict())


async def count_entities(
    conn: Conn,
    dataset: Optional[Dataset] = None,
    unique: Optional[bool] = None,
    target: Optional[bool] = None,
) -> int:
    q = select(func.count(func.distinct(stmt_table.c.canonical_id)))
    q = q.filter(stmt_table.c.prop == BASE)
    if unique is not None:
        q = q.filter(stmt_table.c.unique == unique)
    if target is not None:
        q = q.filter(stmt_table.c.target == target)
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    return await conn.scalar(q)


async def agg_targets_by_country(conn: Conn, dataset: Optional[Dataset] = None):
    """Return the number of targets grouped by country."""
    count = func.count(func.distinct(stmt_table.c.canonical_id))
    q = select(stmt_table.c.value, count)
    # TODO: this could be generic to type values?
    q = q.filter(stmt_table.c.target == True)  # noqa
    q = q.filter(stmt_table.c.prop_type == registry.country.name)
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    q = q.group_by(stmt_table.c.value)
    q = q.order_by(count.desc())
    res = await conn.execute(q)
    countries = []
    for code, count in res.all():
        result = {
            "code": code,
            "count": count,
            "label": registry.country.caption(code),
        }
        countries.append(result)
    return countries


async def agg_targets_by_schema(conn: Conn, dataset: Optional[Dataset] = None):
    """Return the number of targets grouped by their schema."""
    # FIXME: duplicates entities when there are statements with different schema
    # defined for the same entity.
    count = func.count(func.distinct(stmt_table.c.canonical_id))
    q = select(stmt_table.c.schema, count)
    q = q.filter(stmt_table.c.target == True)  # noqa
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    q = q.group_by(stmt_table.c.schema)
    q = q.order_by(count.desc())
    res = await conn.execute(q)
    schemata = []
    for name, count in res.all():
        schema = model.get(name)
        if schema is None:
            continue
        result = {
            "name": name,
            "count": count,
            "label": schema.label,
            "plural": schema.plural,
        }
        schemata.append(result)
    return schemata


async def all_schemata(conn: Conn, dataset: Optional[Dataset] = None):
    """Return all schemata present in the dataset"""
    q = select(func.distinct(stmt_table.c.schema))
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    q = q.group_by(stmt_table.c.schema)
    res = await conn.execute(q)
    return [s for (s,) in res.all()]


async def max_last_seen(
    conn: Conn, dataset: Optional[Dataset] = None
) -> Optional[datetime]:
    """Return the latest date of the data."""
    q = select(func.max(stmt_table.c.last_seen))
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    return await conn.scalar(q)


async def entities_datasets(conn: Conn, dataset: Optional[Dataset] = None):
    """Return all entity IDs with the dataset they belong to."""
    q = select(stmt_table.c.entity_id, stmt_table.c.dataset)
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset.in_(dataset.source_names))
    q = q.distinct()
    result = await conn.stream(q)
    async for row in result:
        entity_id, dataset = row
        yield (entity_id, dataset)


async def cleanup_dataset(conn: Conn, dataset: Dataset):
    # set the entity BASE to the earliest spotting of the entity:
    # table = stmt_table.c.__table__
    # cte = select(
    #     func.min(table.c.first_seen).label("first_seen"),
    #     table.c.entity_id.label("entity_id"),
    # )
    # cte = cte.where(table.c.dataset == dataset.name)
    # cte = cte.group_by(table.c.entity_id)
    # cte = cte.cte("seen")
    # sq = select(cte.c.first_seen)
    # sq = sq.where(cte.c.entity_id == table.c.entity_id)
    # sq = sq.limit(1)
    # q = update(table)
    # q = q.where(table.c.dataset == dataset.name)
    # q = q.where(table.c.prop == BASE)
    # q = q.values({table.c.first_seen: sq.scalar_subquery()})
    # # log.info("Setting BASE first_seen...", q=str(q))
    # db.session.execute(q)

    # remove non-current statements (in the future we may want to keep them?)
    last_seen = await max_last_seen(conn, dataset=dataset)
    if last_seen is not None:
        pq = delete(stmt_table)
        pq = pq.where(stmt_table.c.dataset == dataset.name)
        pq = pq.where(stmt_table.c.last_seen < last_seen)
        await conn.execute(pq)


async def resolve_all_canonical(conn: Conn, resolver: Resolver):
    log.info("Resolving canonical_id in statements...", resolver=resolver)
    await conn.execute(delete(canonical_table))
    mappings = []
    for canonical in resolver.canonicals():
        for referent in resolver.get_referents(canonical, canonicals=False):
            mappings.append({"entity_id": referent, "canonical_id": canonical.id})
        if len(mappings) > 5000:
            # log.info("INSERTING mappings...")
            stmt = insert(canonical_table).values(mappings)
            await conn.execute(stmt)
            mappings = []

    if len(mappings):
        stmt = insert(canonical_table).values(mappings)
        await conn.execute(stmt)

    q = update(stmt_table)
    q = q.where(stmt_table.c.canonical_id != stmt_table.c.entity_id)
    nested_q = select(canonical_table.c.entity_id)
    nested_q = nested_q.where(canonical_table.c.entity_id == stmt_table.c.entity_id)
    q = q.where(~nested_q.exists())
    q = q.values({stmt_table.c.canonical_id: stmt_table.c.entity_id})
    await conn.execute(q)

    q = update(stmt_table)
    q = q.where(stmt_table.c.entity_id == canonical_table.c.entity_id)
    q = q.where(stmt_table.c.canonical_id != canonical_table.c.canonical_id)
    q = q.values({stmt_table.c.canonical_id: canonical_table.c.canonical_id})
    await conn.execute(q)


async def resolve_canonical(conn: Conn, resolver: Resolver, canonical_id: str):
    referents = resolver.get_referents(canonical_id)
    log.debug("Resolving: %s" % canonical_id, referents=referents)
    q = update(stmt_table)
    q = q.where(stmt_table.c.entity_id.in_(referents))
    q = q.values({stmt_table.c.canonical_id: canonical_id})
    await conn.execute(q)


async def clear_statements(conn: Conn, dataset: Optional[Dataset] = None):
    q = delete(stmt_table)
    # TODO: should this do collections?
    if dataset is not None:
        q = q.filter(stmt_table.c.dataset == dataset.name)
    await conn.execute(q)


async def unique_conflict(conn: Conn, left_ids, right_ids):
    cteq = select(
        func.distinct(stmt_table.c.entity_id).label("entity_id"),
        func.max(stmt_table.c.unique).label("unique"),
        stmt_table.c.dataset.label("dataset"),
    )
    cteq = cteq.where(stmt_table.c.prop == BASE)
    cte = cteq.cte("uniques")
    # sqlite 3.35 -
    # cte = cte.prefix_with("MATERIALIZED")
    left = cte.alias("left")
    right = cte.alias("right")
    q = select([left.c.entity_id, right.c.entity_id])
    q = q.where(left.c.dataset == right.c.dataset)
    q = q.where(left.c.unique == True)
    q = q.where(right.c.unique == True)
    q = q.where(left.c.entity_id.in_(left_ids))
    q = q.where(right.c.entity_id.in_(right_ids))
    # print(q)
    res = await conn.execute(q)
    data = await res.fetchone()
    if data is not None:
        return True
    return False