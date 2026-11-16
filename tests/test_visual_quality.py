import unittest

from visual_quality import _is_quota_exhaustion_error


class CloudflareQuotaDetectionTests(unittest.TestCase):
    def test_detects_daily_free_allocation_message(self):
        self.assertTrue(_is_quota_exhaustion_error(
            "Cloudflare visual review failed (429): you have used up your daily free allocation"
        ))

    def test_detects_cloudflare_error_code_4006(self):
        self.assertTrue(_is_quota_exhaustion_error(
            'Cloudflare response: {"success":false,"errors":[{"code":4006}]}'
        ))

    def test_is_case_insensitive(self):
        self.assertTrue(_is_quota_exhaustion_error(
            "YOU HAVE USED UP YOUR DAILY FREE ALLOCATION"
        ))

    def test_does_not_classify_ordinary_transient_errors_as_quota(self):
        self.assertFalse(_is_quota_exhaustion_error(
            "Cloudflare visual review failed (503): service temporarily unavailable"
        ))

    def test_does_not_classify_invalid_model_error_as_quota(self):
        self.assertFalse(_is_quota_exhaustion_error(
            "Cloudflare visual review failed (400): model not found"
        ))


if __name__ == "__main__":
    unittest.main()
