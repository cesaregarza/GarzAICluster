import inspect
import unittest

from scripts import provision_agent_control_plane_secrets as provision


class RuntimeRoleSqlTests(unittest.TestCase):
    def test_runtime_role_keeps_temporary_database_privilege(self) -> None:
        source = inspect.getsource(provision.run_sql)

        self.assertIn("REVOKE ALL ON DATABASE %I FROM %I", source)
        self.assertIn("GRANT CONNECT ON DATABASE %I TO %I", source)
        self.assertIn("GRANT CREATE ON DATABASE %I TO %I", source)
        self.assertIn("GRANT TEMPORARY ON DATABASE %I TO %I", source)


if __name__ == "__main__":
    unittest.main()
