"""Tests for aws_cleaner/tools/deleter.py

Run:
    python3 test_deletion.py
    python3 test_deletion.py --filter dry_run
"""
import argparse
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from aws_cleaner.tools.deleter import (
    delete_resources,
    render_audit_log,
    _delete_s3_bucket,
    _delete_ecr_images,
    _delete_ebs_volume,
    _terminate_ec2_instance,
    _empty_s3_bucket,
)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def make_session():
    return MagicMock(name="boto3.Session")


def make_rec(service, resource_id, savings=10.0, reason="unused"):
    return {
        "service": service,
        "resource_id": resource_id,
        "monthly_savings_usd": savings,
        "reason": reason,
    }


# ─── Tests ─────────────────────────────────────────────────────────────────────

class TestDryRunMode(unittest.TestCase):
    """delete_resources with execute=False should never call AWS."""

    def test_dry_run_returns_dry_run_status(self):
        session = make_session()
        recs = [make_rec("s3", "my-bucket")]
        audit = delete_resources(session, recs, execute=False, force=True, interactive=False)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["status"], "dry_run")

    def test_dry_run_does_not_call_delete_bucket(self):
        session = make_session()
        recs = [make_rec("s3", "my-bucket")]
        delete_resources(session, recs, execute=False, force=True, interactive=False)
        # client() should never be called in dry-run
        session.client.assert_not_called()

    def test_dry_run_multiple_services(self):
        session = make_session()
        recs = [
            make_rec("s3", "bucket-a"),
            make_rec("ebs", "vol-1234"),
            make_rec("ecr", "my-repo"),
            make_rec("ec2", "i-1234"),
        ]
        audit = delete_resources(session, recs, execute=False, force=True, interactive=False)
        self.assertEqual(len(audit), 4)
        for entry in audit:
            self.assertEqual(entry["status"], "dry_run")
        session.client.assert_not_called()

    def test_dry_run_empty_recommendations(self):
        session = make_session()
        audit = delete_resources(session, [], execute=False, force=True, interactive=False)
        self.assertEqual(audit, [])

    def test_dry_run_preserves_savings(self):
        session = make_session()
        recs = [make_rec("ebs", "vol-9999", savings=42.50)]
        audit = delete_resources(session, recs, execute=False, force=True, interactive=False)
        self.assertAlmostEqual(audit[0]["monthly_savings_usd"], 42.50)


class TestS3Deletion(unittest.TestCase):
    """_delete_s3_bucket empties bucket then deletes it."""

    def _make_s3_client(self, objects=None, versions=None):
        s3 = MagicMock()
        # list_objects_v2 paginator
        obj_page = {"Contents": objects or []}
        obj_paginator = MagicMock()
        obj_paginator.paginate.return_value = [obj_page]

        # list_object_versions paginator
        ver_page = {"Versions": versions or [], "DeleteMarkers": []}
        ver_paginator = MagicMock()
        ver_paginator.paginate.return_value = [ver_page]

        s3.get_paginator.side_effect = lambda name: (
            obj_paginator if name == "list_objects_v2" else ver_paginator
        )
        return s3

    def test_empty_bucket_deleted(self):
        s3 = self._make_s3_client(objects=[])
        session = make_session()
        session.client.return_value = s3
        rec = make_rec("s3", "empty-bucket")
        result = _delete_s3_bucket(session, rec)
        s3.delete_bucket.assert_called_once_with(Bucket="empty-bucket")
        self.assertIn("empty-bucket", result)

    def test_non_empty_bucket_empties_then_deletes(self):
        objects = [{"Key": "file1.txt"}, {"Key": "file2.txt"}]
        s3 = self._make_s3_client(objects=objects)
        session = make_session()
        session.client.return_value = s3
        rec = make_rec("s3", "full-bucket")
        _delete_s3_bucket(session, rec)
        # Should have called delete_objects before delete_bucket
        s3.delete_objects.assert_called()
        s3.delete_bucket.assert_called_once_with(Bucket="full-bucket")

    def test_versioned_objects_deleted(self):
        versions = [{"Key": "v-file", "VersionId": "abc123"}]
        s3 = self._make_s3_client(objects=[], versions=versions)
        session = make_session()
        session.client.return_value = s3
        rec = make_rec("s3", "versioned-bucket")
        _delete_s3_bucket(session, rec)
        # delete_objects called for version cleanup
        s3.delete_objects.assert_called()
        s3.delete_bucket.assert_called_once()


class TestECRDeletion(unittest.TestCase):
    """_delete_ecr_images removes all images from a repo."""

    def test_images_batch_deleted(self):
        ecr = MagicMock()
        image_ids = [{"imageDigest": f"sha256:{i:064d}"} for i in range(5)]
        paginator = MagicMock()
        paginator.paginate.return_value = [{"imageIds": image_ids}]
        ecr.get_paginator.return_value = paginator
        session = make_session()
        session.client.return_value = ecr

        rec = make_rec("ecr", "my-service-repo")
        result = _delete_ecr_images(session, rec)
        ecr.batch_delete_image.assert_called_once_with(
            repositoryName="my-service-repo", imageIds=image_ids
        )
        self.assertIn("5", result)

    def test_empty_repo_no_delete_call(self):
        ecr = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"imageIds": []}]
        ecr.get_paginator.return_value = paginator
        session = make_session()
        session.client.return_value = ecr

        rec = make_rec("ecr", "empty-repo")
        result = _delete_ecr_images(session, rec)
        ecr.batch_delete_image.assert_not_called()
        self.assertIn("0", result)

    def test_large_repo_batched_in_100s(self):
        ecr = MagicMock()
        image_ids = [{"imageDigest": f"sha256:{i:064d}"} for i in range(250)]
        paginator = MagicMock()
        paginator.paginate.return_value = [{"imageIds": image_ids}]
        ecr.get_paginator.return_value = paginator
        session = make_session()
        session.client.return_value = ecr

        rec = make_rec("ecr", "large-repo")
        _delete_ecr_images(session, rec)
        # Should have called batch_delete_image 3 times (100, 100, 50)
        self.assertEqual(ecr.batch_delete_image.call_count, 3)


class TestEBSDeletion(unittest.TestCase):
    def test_volume_deleted(self):
        ec2 = MagicMock()
        session = make_session()
        session.client.return_value = ec2
        rec = make_rec("ebs", "vol-0abc1234")
        result = _delete_ebs_volume(session, rec)
        ec2.delete_volume.assert_called_once_with(VolumeId="vol-0abc1234")
        self.assertIn("vol-0abc1234", result)


class TestEC2Deletion(unittest.TestCase):
    def test_instance_terminated(self):
        ec2 = MagicMock()
        session = make_session()
        session.client.return_value = ec2
        rec = make_rec("ec2", "i-0abc1234deadbeef")
        result = _terminate_ec2_instance(session, rec)
        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-0abc1234deadbeef"])
        self.assertIn("i-0abc1234deadbeef", result)


class TestLiveExecutionMode(unittest.TestCase):
    """execute=True with force=True triggers actual AWS calls."""

    def test_s3_execute_calls_delete_bucket(self):
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        ver_paginator = MagicMock()
        ver_paginator.paginate.return_value = [{"Versions": [], "DeleteMarkers": []}]
        s3.get_paginator.side_effect = lambda name: (
            paginator if name == "list_objects_v2" else ver_paginator
        )
        session = make_session()
        session.client.return_value = s3

        recs = [make_rec("s3", "bucket-to-delete")]
        audit = delete_resources(session, recs, execute=True, force=True, interactive=False)
        self.assertEqual(audit[0]["status"], "deleted")
        s3.delete_bucket.assert_called_once_with(Bucket="bucket-to-delete")

    def test_error_handled_gracefully(self):
        from botocore.exceptions import ClientError
        ec2 = MagicMock()
        ec2.delete_volume.side_effect = ClientError(
            {"Error": {"Code": "InvalidVolume.NotFound", "Message": "Volume not found"}},
            "DeleteVolume",
        )
        session = make_session()
        session.client.return_value = ec2

        recs = [make_rec("ebs", "vol-missing")]
        audit = delete_resources(session, recs, execute=True, force=True, interactive=False)
        self.assertEqual(audit[0]["status"], "error")
        self.assertIn("InvalidVolume.NotFound", audit[0]["message"])

    def test_unsupported_service_returns_error(self):
        session = make_session()
        recs = [make_rec("rds", "db-instance-xyz")]
        audit = delete_resources(session, recs, execute=True, force=True, interactive=False)
        self.assertEqual(audit[0]["status"], "error")


class TestAuditLogRender(unittest.TestCase):
    """render_audit_log shouldn't throw with any input."""

    def test_render_empty(self):
        render_audit_log([])  # no exception

    def test_render_mixed_statuses(self):
        log = [
            {"service": "s3", "resource_id": "b1", "status": "deleted", "message": "ok", "monthly_savings_usd": 5.0},
            {"service": "ebs", "resource_id": "v1", "status": "error", "message": "err", "monthly_savings_usd": 0},
            {"service": "ecr", "resource_id": "r1", "status": "dry_run", "message": "dry", "monthly_savings_usd": 2.0},
        ]
        render_audit_log(log)  # no exception

    def test_render_missing_fields(self):
        log = [{"status": "dry_run"}]
        render_audit_log(log)  # graceful with missing keys


# ─── Runner ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deletion engine tests")
    parser.add_argument("--filter", help="Only run tests matching this string")
    args, _ = parser.parse_known_args()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestDryRunMode,
        TestS3Deletion,
        TestECRDeletion,
        TestEBSDeletion,
        TestEC2Deletion,
        TestLiveExecutionMode,
        TestAuditLogRender,
    ]

    for cls in test_classes:
        tests = loader.loadTestsFromTestCase(cls)
        if args.filter:
            tests = unittest.TestSuite(
                t for t in tests if args.filter.lower() in t._testMethodName.lower()
            )
        suite.addTests(tests)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
