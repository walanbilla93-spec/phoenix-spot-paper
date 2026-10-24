"""Disposable CI PostgreSQL integration. Never points at the deployment database."""
import copy
import os
import unittest
from phoenix import Store, initial

@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'disposable PostgreSQL not configured')
class PostgresTest(unittest.TestCase):
    def test_durable_transaction_restart_dedup_and_lock(self):
        url=os.environ['TEST_DATABASE_URL'];db=Store(url)
        with self.assertRaises(ValueError):Store(url) # advisory single-writer lock
        def change(st):st['observations']+=1;return [{'type':'synthetic_integration'}]
        self.assertTrue(db.apply('pg-integration-1',change))
        state=copy.deepcopy(db.state);head=db.verify();db.db.close()
        db=Store(url);self.assertEqual(db.state,state);self.assertEqual(db.verify(),head)
        self.assertFalse(db.apply('pg-integration-1',change))
        def crash(st):st['observations']+=100;raise RuntimeError('synthetic crash')
        with self.assertRaises(RuntimeError):db.apply('pg-rollback',crash)
        self.assertEqual(db.state,state);self.assertEqual(db.verify(),head)
        with self.assertRaises(Exception):db.db.execute('DELETE FROM events')
        db.db.close()

    def test_two_namespaces_have_independent_locks_and_ledgers(self):
        from rotation import weekly_initial,weekly_audit
        url=os.environ['TEST_DATABASE_URL'];fixed=Store(url)
        before=copy.deepcopy(fixed.state);head=fixed.verify()
        weekly=Store(url,factory=weekly_initial,validator=weekly_audit,schema='phoenix_weekly')
        def change(st):st['observations']+=1;return [{'type':'weekly_integration'}]
        self.assertTrue(weekly.apply('pg-weekly-1',change));saved=copy.deepcopy(weekly.state)
        self.assertEqual(fixed.state,before);self.assertEqual(fixed.verify(),head)
        with self.assertRaises(ValueError):Store(url,factory=weekly_initial,validator=weekly_audit,schema='phoenix_weekly')
        weekly.db.close()
        weekly=Store(url,factory=weekly_initial,validator=weekly_audit,schema='phoenix_weekly')
        self.assertEqual(weekly.state,saved);weekly.verify()
        weekly.db.close();fixed.db.close()

if __name__=='__main__':unittest.main(verbosity=2)
