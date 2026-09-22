from contextlib import contextmanager

from core.test_helpers import create_test_interactive_user, create_admin_role
from deduplication.apps import DeduplicationConfig


@contextmanager
def override_deduplication_config(**overrides):
    """Temporarily set DeduplicationConfig class attributes (e.g. demographic_columns)."""
    previous = {key: getattr(DeduplicationConfig, key) for key in overrides}
    for key, value in overrides.items():
        setattr(DeduplicationConfig, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            setattr(DeduplicationConfig, key, value)


class LogInHelper:
    _TEST_USER_NAME = "TestUserTest2"
    # Must satisfy CustomPasswordValidator: 8+ chars, upper, lower, digit, special.
    _TEST_USER_PASSWORD = "TestPasswordTest2!"

    def get_or_create_user_api(self):
        # Built lazily (not as a class attribute) so creating the admin role only
        # touches the DB once a test is actually running, not at import time.
        test_data_user = {
            "last_name": self._TEST_USER_NAME,
            "other_names": self._TEST_USER_NAME,
            "user_types": "INTERACTIVE",
            "roles": [create_admin_role().id],
        }
        return create_test_interactive_user(
            username=self._TEST_USER_NAME,
            password=self._TEST_USER_PASSWORD,
            custom_props=test_data_user,
        )
