"""Producing an archive refuses every stored secret that does not open, by location only."""

from pathlib import Path

import pytest

from app.backup import paths, producer
from app.backup.manifest import ProductionFailed
from tests.backup.support import UNDECRYPTABLE_OLD, clock, make_world
from tests.crypto.helpers import tamper

UNPARSEABLE = "not-a-token"
CURRENT_VERSION_ROW = 2
OLD_VERSION_ROW = 1
CORRUPT_LOCATION = f"registry_credential.secret_ciphertext row {CURRENT_VERSION_ROW}"
OLD_LOCATION = f"registry_credential.secret_ciphertext row {OLD_VERSION_ROW}"
UNPARSEABLE_LOCATION = "object_storage_target.secret_access_key_ciphertext row 1"
SECRET_PLAINTEXTS = ("plain-new", "plain-registry_credential-secret_ciphertext")


async def test_a_corrupt_an_unparseable_and_an_unreadable_secret_each_refuse_the_backup_by_location(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(tmp_path)
    current = world.database.connect()
    try:
        sealed = current.execute(
            "SELECT secret_ciphertext FROM registry_credential WHERE id = ?",
            (CURRENT_VERSION_ROW,),
        ).fetchone()[0]
    finally:
        current.close()
    corrupt = tamper(sealed)
    world.run(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (corrupt, CURRENT_VERSION_ROW),
    )
    world.run(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (UNDECRYPTABLE_OLD, OLD_VERSION_ROW),
    )
    world.run("UPDATE object_storage_target SET secret_access_key_ciphertext = ?", (UNPARSEABLE,))
    workdir = paths.new_work_directory(world.data_dir)

    try:
        with pytest.raises(ProductionFailed) as raised:
            await producer.produce_archive(world.database, world.vault, workdir, clock=clock)
    finally:
        await world.database.aclose()

    message = raised.value.message
    [record] = [r for r in caplog.records if r.getMessage() == "backup_secret_unreadable"]
    logged = str(vars(record))
    assert raised.value.reason == "secret_unreadable"
    for location in (CORRUPT_LOCATION, OLD_LOCATION, UNPARSEABLE_LOCATION):
        assert location in message
        assert location in logged
    for value in (corrupt, sealed, UNDECRYPTABLE_OLD, UNPARSEABLE, *SECRET_PLAINTEXTS):
        assert value not in message
        assert value not in logged
    assert list(workdir.iterdir()) == []
