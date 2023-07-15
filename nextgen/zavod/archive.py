import os
import csv
from pathlib import Path
from functools import cache
from typing import Optional, Generator, TextIO, Union
from zavod.logs import get_logger
from google.cloud.storage import Client, Bucket, Blob  # type: ignore
from nomenklatura.statement import Statement
from nomenklatura.statement.serialize import unpack_row

from zavod import settings
from zavod.meta.dataset import Dataset

log = get_logger(__name__)
StatementGen = Generator[Statement, None, None]
PathLike = Union[str, os.PathLike[str]]
BLOB_CHUNK = 40 * 1024 * 1024
STATEMENTS_RESOURCE = "statements.pack"
ISSUES_LOG_RESOURCE = "issues.log.json"
INDEX_RESOURCE = "index.json"


@cache
def get_archive_bucket() -> Optional[Bucket]:
    if settings.ARCHIVE_BUCKET is None:
        log.warn("No backfill bucket configured")
        return None
    client = Client()
    bucket = client.get_bucket(settings.ARCHIVE_BUCKET)
    return bucket


def get_backfill_blob(dataset_name: str, resource: PathLike) -> Optional[Blob]:
    bucket = get_archive_bucket()
    if bucket is None:
        return None
    blob_name = f"datasets/{settings.BACKFILL_RELEASE}/{dataset_name}/{resource}"
    return bucket.get_blob(blob_name)


def backfill_resource(
    dataset_name: str, resource: PathLike, path: Path
) -> Optional[Path]:
    blob = get_backfill_blob(dataset_name, resource)
    if blob is not None:
        log.info(
            "Backfilling dataset resource...",
            dataset=dataset_name,
            resource=resource,
            blob_name=blob.name,
        )
        blob.download_to_filename(path)
        return path
    return None


def dataset_path(dataset_name: str) -> Path:
    path = settings.DATASET_PATH / dataset_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def dataset_resource_path(dataset_name: str, resource: PathLike) -> Path:
    return dataset_path(dataset_name).joinpath(resource)


def get_dataset_resource(
    dataset: Dataset,
    resource: PathLike,
    backfill: bool = True,
    force_backfill: bool = False,
) -> Optional[Path]:
    path = dataset_resource_path(dataset.name, resource)
    if path.exists() and not force_backfill:
        return path
    if backfill or force_backfill:
        return backfill_resource(dataset.name, resource, path)
    return None


def get_dataset_index(dataset_name: str, backfill: bool = True) -> Optional[Path]:
    path: Optional[Path] = dataset_resource_path(dataset_name, INDEX_RESOURCE)
    if path is not None and not path.exists() and backfill:
        path = backfill_resource(dataset_name, INDEX_RESOURCE, path)
    if path is not None and path.exists():
        return path
    return None


def read_fh_statements(fh: TextIO, external: bool) -> StatementGen:
    for cells in csv.reader(fh):
        stmt = unpack_row(cells, Statement)
        if not external and stmt.external:
            continue
        yield stmt


def iter_dataset_statements(dataset: Dataset, external: bool = True) -> StatementGen:
    for scope in dataset.leaves:
        yield from _iter_scope_statements(scope, external=external)


def _iter_scope_statements(dataset: Dataset, external: bool = True) -> StatementGen:
    path = dataset_resource_path(dataset.name, STATEMENTS_RESOURCE)
    if not path.exists():
        backfill_blob = get_backfill_blob(dataset.name, STATEMENTS_RESOURCE)
        if backfill_blob is not None:
            log.info(
                "Streaming backfilled statements...",
                dataset=dataset.name,
            )
            with backfill_blob.open("r", chunk_size=BLOB_CHUNK) as fh:
                yield from read_fh_statements(fh, external)
            return
        raise ValueError(f"Cannot load statements for: {dataset.name}")

    with open(path, "r") as fh:
        yield from read_fh_statements(fh, external)
