"""Regression for client-buffered PostgreSQL reads and concurrent exports."""
import hashlib,json,os,tempfile,tracemalloc,unittest,threading,urllib.request,urllib.error
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from phoenix import Store,canonical
import service_v2 as app

class BufferedCursor:
    def __init__(self,rows):self.rows=rows
    def __iter__(self):return iter(self.rows)
    def fetchone(self):return self.rows[0] if self.rows else None
    def close(self):self.rows=[]

class BufferedDriver:
    """Model psycopg's default fully buffered SELECT using real SQLite rows."""
    def __init__(self,connection):self.connection=connection;self.max_rows=0
    def execute(self,sql,params=()):
        rows=self.connection.execute(sql,params).fetchall()
        self.max_rows=max(self.max_rows,len(rows))
        return BufferedCursor(rows)

class MemoryTest(unittest.TestCase):
    def test_large_ledger_verifies_with_bounded_driver_memory_and_checks_every_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Store(os.path.join(folder,'large.db'));connection=db.db
            payload=canonical(dict(events=[dict(type='synthetic',input='x'*32768)],after=db.state))
            prev='0'*64
            connection.execute('BEGIN')
            for i in range(1200):
                key=str(i);digest=hashlib.sha256((prev+key+payload).encode()).hexdigest()
                connection.execute('INSERT INTO events(observation,payload,previous,hash) VALUES(?,?,?,?)',(key,payload,prev,digest));prev=digest
            connection.execute('COMMIT');db.db=BufferedDriver(connection)
            tracemalloc.start()
            try:
                self.assertEqual(db.verify(),prev)
                _,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertLess(peak,4*1024*1024)
            self.assertLessEqual(db.db.max_rows,4)
            print('buffered-driver verification: 1200 x 32 KiB events; peak Python bytes =',peak)
            # A bad appended hash must still be detected; nothing is skipped.
            connection.execute('INSERT INTO events(observation,payload,previous,hash) VALUES(?,?,?,?)',('bad',payload,prev,'f'*64))
            with self.assertRaisesRegex(ValueError,'integrity'):db.verify()
            connection.close()

    def test_health_survives_export_backpressure_and_gate_releases(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            app.READ_RESPONSE.acquire()
            try:
                with urllib.request.urlopen(base+'/healthz') as response:
                    self.assertTrue(json.load(response)['alive'])
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(base+'/api/trades')
                self.assertEqual(error.exception.code,503)
                self.assertEqual(error.exception.headers['Retry-After'],'2')
            finally:app.READ_RESPONSE.release()
            with urllib.request.urlopen(base+'/api/trades') as response:self.assertFalse(json.load(response)['ready'])
        finally:server.shutdown();server.server_close()

    def test_full_proof_export_reads_one_evidence_record_at_a_time(self):
        records=[dict(record_id=str(i),ledger_id=i) for i in range(200)]
        def proof(db,record):return dict(record,evidence={'book':'x'*65536})
        tracemalloc.start()
        try:
            with patch.object(app,'trade_evidence',side_effect=proof):
                size=sum(len(chunk) for chunk in app.response_chunks({'records':records},object()))
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertGreater(size,12*1024*1024)
        self.assertLess(peak,2*1024*1024)
