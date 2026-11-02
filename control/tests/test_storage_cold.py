"""Cold-class storage: set_storage_class, restore, restore_state and get_file, as call shapes against mock clients
(no network) and as behaviour on the local backend. Real retrieval times and cost are not tested: they depend on the provider."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from volt import storage
from volt.storage import AzureBlobBackend, AzureConfig, GcsBackend, GcsConfig, LocalBackend, S3Backend, S3Config, StorageError


class Local(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = LocalBackend(Path(self.tmp.name) / "store")
        src = Path(self.tmp.name) / "a.bin"
        src.write_bytes(b"hello archive")
        self.b.put_file(src, "x/a.bin")

    def test_cold_and_back(self):
        self.assertEqual(self.b.restore_state("x/a.bin"), "online")
        self.b.set_storage_class("x/a.bin", "cold")
        self.assertEqual(self.b.restore_state("x/a.bin"), "archived")
        self.assertEqual(list(self.b.list_keys()), ["x/a.bin"])   # the marker is not an object
        self.b.restore("x/a.bin", 7)
        self.assertEqual(self.b.restore_state("x/a.bin"), "online")

    def test_get_file_and_missing_keys(self):
        dest = Path(self.tmp.name) / "out.bin"
        self.b.get_file("x/a.bin", dest)
        self.assertEqual(dest.read_bytes(), b"hello archive")
        with self.assertRaises(StorageError):
            self.b.get_file("x/none.bin", dest)
        with self.assertRaises(StorageError):
            self.b.set_storage_class("x/none.bin", "cold")
        with self.assertRaises(StorageError):
            self.b.restore_state("x/none.bin")

    def test_delete_removes_the_marker_too(self):
        self.b.set_storage_class("x/a.bin", "cold")
        self.b.delete("x/a.bin")
        self.assertFalse(list(Path(self.tmp.name, "store").rglob("*.cold")))


class S3(unittest.TestCase):
    def backend(self, client):
        return S3Backend(S3Config(bucket="b", prefix="pre"), client=client)

    def test_call_shapes(self):
        c = mock.MagicMock()
        b = self.backend(c)
        b.set_storage_class("k/a.mxf", "DEEP_ARCHIVE")
        c.copy.assert_called_once_with({"Bucket": "b", "Key": "pre/k/a.mxf"}, "b", "pre/k/a.mxf",
                                       ExtraArgs={"StorageClass": "DEEP_ARCHIVE", "MetadataDirective": "COPY"})
        b.restore("k/a.mxf", 5)
        c.restore_object.assert_called_once_with(Bucket="b", Key="pre/k/a.mxf",
                                                 RestoreRequest={"Days": 5, "GlacierJobParameters": {"Tier": "Standard"}})
        b.get_file("k/a.mxf", "/tmp/x")
        c.download_file.assert_called_once_with("b", "pre/k/a.mxf", "/tmp/x")

    def test_states(self):
        for head, want in (({}, "online"), ({"StorageClass": "STANDARD_IA"}, "online"), ({"StorageClass": "DEEP_ARCHIVE"}, "archived"),
                           ({"StorageClass": "GLACIER", "Restore": 'ongoing-request="true"'}, "restoring"),
                           ({"StorageClass": "GLACIER", "Restore": 'ongoing-request="false", expiry-date="Fri, 21 Dec 2026 00:00:00 GMT"'}, "online")):
            c = mock.MagicMock()
            c.head_object.return_value = head
            self.assertEqual(self.backend(c).restore_state("k"), want, head)

    def test_a_second_restore_request_is_not_an_error(self):
        c = mock.MagicMock()
        c.restore_object.side_effect = Exception("An error occurred (RestoreAlreadyInProgress) when calling RestoreObject")
        self.backend(c).restore("k")   # does not raise
        c.restore_object.side_effect = Exception("boom")
        with self.assertRaises(StorageError):
            self.backend(c).restore("k")


class Azure(unittest.TestCase):
    def setUp(self):
        self.blob = mock.MagicMock()
        service = mock.MagicMock()
        service.get_blob_client.return_value = self.blob
        self.b = AzureBlobBackend(AzureConfig(account="acct", container="c", prefix="pre"), client=service)

    def test_call_shapes(self):
        self.b.set_storage_class("k/a", "Archive")
        self.blob.set_standard_blob_tier.assert_called_once_with("Archive")
        self.blob.reset_mock()
        self.b.restore("k/a")
        self.blob.set_standard_blob_tier.assert_called_once_with("Hot", rehydrate_priority="Standard")

    def test_states(self):
        for props, want in ((SimpleNamespace(blob_tier="Hot", archive_status=None), "online"),
                            (SimpleNamespace(blob_tier="Archive", archive_status=None), "archived"),
                            (SimpleNamespace(blob_tier="Archive", archive_status="rehydrate-pending-to-hot"), "restoring")):
            self.blob.get_blob_properties.return_value = props
            self.assertEqual(self.b.restore_state("k/a"), want)


class Gcs(unittest.TestCase):
    def setUp(self):
        self.blob = mock.MagicMock()
        client = mock.MagicMock()
        client.bucket.return_value.blob.return_value = self.blob
        self.b = GcsBackend(GcsConfig(bucket="b", prefix="pre"), client=client)

    def test_class_change_and_immediate_reads(self):
        self.b.set_storage_class("k/a", "ARCHIVE")
        self.blob.update_storage_class.assert_called_once_with("ARCHIVE")
        self.assertEqual(self.b.restore_state("k/a"), "online")
        self.assertIsNone(self.b.restore("k/a"))
        self.b.get_file("k/a", "/tmp/y")
        self.blob.download_to_filename.assert_called_once_with("/tmp/y")


class Defaults(unittest.TestCase):
    def test_each_backend_names_its_cold_class(self):
        self.assertEqual((LocalBackend.cold_class, S3Backend.cold_class, AzureBlobBackend.cold_class, GcsBackend.cold_class),
                         ("cold", "DEEP_ARCHIVE", "Archive", "ARCHIVE"))


if __name__ == "__main__":
    unittest.main()
