import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import wifi_sd


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, data, status=200):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        if isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def do_GET(self):
        self.server.requests.append(('GET', self.path))
        if self.server.delay:
            time.sleep(self.server.delay)
        if self.server.redirect:
            self.send('', 302)
        elif self.path.startswith('/command?'):
            command = parse_qs(urlsplit(self.path).query)['plain'][0]
            self.send(self.server.capability if command == '[ESP800]' else self.server.card)
        elif self.path.startswith('/upload?'):
            self.send(self.server.listing)
        elif self.path.startswith('/SD/'):
            self.send(self.server.saved if not self.server.corrupt else b'corrupt')
        else:
            self.send('', 404)

    def do_POST(self):
        self.server.requests.append(('POST', self.path))
        self.server.body = self.rfile.read(int(self.headers['Content-Length']))
        boundary = self.headers['Content-Type'].split('boundary=')[1].encode()
        parts = self.server.body.split(b'--' + boundary)
        payload = next(x for x in parts if b'name="myfile[]"' in x)
        self.server.saved = payload.split(b'\r\n\r\n', 1)[1][:-2]
        self.send(self.server.upload_result)


class Tests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        self.server.requests, self.server.saved = [], b''
        self.server.capability = 'FW version:1 # FW target:marlin-embedded  # FW HW:Direct SD # primary sd:/sd'
        self.server.card = 'SD card detected'
        self.server.listing = {'status': 'Ok', 'mode': 'direct', 'path': '/', 'files': []}
        self.server.upload_result = {'status': 'Ok', 'mode': 'direct'}
        self.server.corrupt = self.server.redirect = False
        self.server.delay = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = wifi_sd.Client('127.0.0.1:%d' % self.server.server_port)
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / 'example.gcode'
        self.file.write_bytes(b'; harmless test\n')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_upload_exact_multipart_and_verify(self):
        digest = self.client.upload(str(self.file), 'TEST.GCO')
        self.assertEqual(digest, hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertIn(b'name="path"\r\n\r\n/', self.server.body)
        self.assertIn(b'name="/TEST.GCOS"\r\n\r\n16', self.server.body)
        self.assertIn(b'name="myfile[]"; filename="/TEST.GCO"', self.server.body)
        self.assertEqual(self.server.saved, self.file.read_bytes())
        self.assertEqual(self.server.requests[-2:], [('POST', '/upload'), ('GET', '/SD/TEST.GCO')])

    def test_invalid_capability_busy_no_card_and_listing_errors(self):
        for cap, card, listing in [
            ('FW HW:No SD', 'SD card detected', self.server.listing),
            ('FW HW:Direct SD', 'Busy', self.server.listing),
            ('FW HW:Direct SD', 'No SD card', self.server.listing),
            ('FW HW:Direct SD', 'SD card detected', {'status':'No SD Card'}),
            ('FW HW:Direct SD', 'SD card detected', {'status':'Ok','mode':'serial','path':'/','files':[]}),
        ]:
            self.server.capability, self.server.card, self.server.listing = cap, card, listing
            with self.assertRaises(wifi_sd.TransferError):
                self.client.upload(str(self.file), 'TEST.GCO')
        self.assertFalse(any(method == 'POST' for method, _ in self.server.requests))

    def test_collision_both_names_case_insensitive(self):
        for entry in [{'name':'test.gco','shortname':'other.gco'}, {'name':'other.gco','shortname':'test.gco'}]:
            self.server.listing['files'] = [entry]
            with self.assertRaisesRegex(wifi_sd.TransferError, 'exists'):
                self.client.upload(str(self.file), 'TEST.GCO')
        self.assertFalse(any(method == 'POST' for method, _ in self.server.requests))

    def test_bad_filename_redirect_and_upload_errors(self):
        for name in ['foo.GCO', '../X.GCO', '123456789.GCO', 'X.GCODE', 'X".GCO']:
            with self.assertRaises(wifi_sd.TransferError):
                self.client.upload(str(self.file), name)
        self.server.redirect = True
        with self.assertRaisesRegex(wifi_sd.TransferError, 'HTTP 302'):
            self.client.probe()
        self.server.redirect = False
        self.server.upload_result = {'status':'Upload failed','mode':'direct'}
        with self.assertRaisesRegex(wifi_sd.TransferError, 'failed'):
            self.client.upload(str(self.file), 'TEST.GCO')
        self.assertFalse(any(path.startswith('/SD/') for _, path in self.server.requests))

    def test_corruption_and_timeout(self):
        self.server.corrupt = True
        with self.assertRaisesRegex(wifi_sd.TransferError, 'mismatch'):
            self.client.upload(str(self.file), 'TEST.GCO')
        self.server.delay = 0.2
        with self.assertRaises((TimeoutError, socket.timeout)):
            wifi_sd.Client('127.0.0.1:%d' % self.server.server_port, timeout=0.05).probe()

    def test_host_validation_size_and_generated_name(self):
        for host in ['http://printer', 'x/path', 'u:p@host', 'host?x=1']:
            with self.assertRaises(wifi_sd.TransferError):
                wifi_sd.Client(host)
        self.assertRegex(wifi_sd.sd_name(str(self.file)), r'^[A-Z0-9_]{1,8}\.GCO$')
        self.file.write_bytes(b'')
        with self.assertRaises(wifi_sd.TransferError):
            self.client.upload(str(self.file), 'TEST.GCO')


if __name__ == '__main__':
    unittest.main()
