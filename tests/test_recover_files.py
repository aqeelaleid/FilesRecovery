import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import recover_files


class RecoverFilesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_scan_copies_matching_files(self):
        source = self.tempdir / "source"
        restore = self.tempdir / "restore"
        source.mkdir()
        (source / "photo.jpg").write_bytes(b"fake")
        (source / "notes.tmp").write_bytes(b"skip")

        count = recover_files.copy_existing_matches(
            paths=[source],
            restore_root=restore,
            extensions={".jpg"},
            preserve_paths=False,
            dry_run=False,
            max_files=None,
            exclude_system_files=True,
        )

        self.assertEqual(count, 1)
        recovered = list((restore / "images").glob("*.jpg"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"fake")

    def test_carves_jpeg_from_image_file(self):
        disk_image = self.tempdir / "disk.img"
        restore = self.tempdir / "restore"
        jpeg = b"\xff\xd8\xff\xe0" + b"A" * 200 + b"\xff\xd9"
        disk_image.write_bytes(b"noise" * 50 + jpeg + b"tail")

        count = recover_files.carve_source(
            source=str(disk_image),
            restore_root=restore,
            formats=[fmt for fmt in recover_files.CARVE_FORMATS if fmt.name == "jpeg"],
            allowed_extensions={".jpg", ".jpeg"},
            filters=recover_files.RecoveryFilters(),
            chunk_size=64,
            dry_run=False,
            max_files=None,
        )

        self.assertEqual(count, 1)
        recovered = list((restore / "images").glob("*.jpg"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), jpeg)

    def test_zip_office_sniff_renames_docx(self):
        path = self.tempdir / "sample.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/document.xml", "<document />")

        renamed = recover_files.sniff_recovered_zip(path)

        self.assertEqual(renamed.suffix, ".docx")
        self.assertTrue(renamed.exists())

    def test_mp4_length_parser(self):
        ftyp = (16).to_bytes(4, "big") + b"ftypisom" + b"\x00\x00\x00\x00"
        mdat = (12).to_bytes(4, "big") + b"mdat" + b"data"
        moov = (8).to_bytes(4, "big") + b"moov"
        payload = ftyp + mdat + moov
        reader = io.BytesIO(payload)

        hit = recover_files.carve_mp4(reader, 0, 1024)

        self.assertIsNotNone(hit)
        self.assertEqual(hit.length, len(payload))
        self.assertEqual(hit.extension, ".mp4")

    def test_user_filters_skip_small_carved_files(self):
        keep, reason = recover_files.should_keep_carved_hit(
            reader=io.BytesIO(b"abc"),
            offset=0,
            hit=recover_files.CarveHit(length=3, extension=".pdf"),
            filters=recover_files.RecoveryFilters(min_size=1024),
        )

        self.assertFalse(keep)
        self.assertIn("smaller", reason)


if __name__ == "__main__":
    unittest.main()
