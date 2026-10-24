import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import service_v2 as app

class HttpTest(unittest.TestCase):
    def test_read_only_modes_and_storage_labels(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for mode in ('fixed','weekly'):
                with urllib.request.urlopen(base+'/api/report?mode='+mode) as response:r=json.load(response)
                self.assertEqual(r['portfolio'],mode);self.assertEqual(r['state']['cash'],1000)
                self.assertFalse(r['health']['real_orders_enabled'])
            with urllib.request.urlopen(base+'/') as response:html=response.read().decode()
            self.assertIn('Weekly rotation',html);self.assertIn('persistent PostgreSQL',html)
            self.assertIn('waiting reason',html)
            with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(base+'/api/report?mode=real')
            self.assertEqual(err.exception.code,400)
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(base+'/api/report',method='POST',data=b'{}'))
            self.assertEqual(err.exception.code,501)
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
