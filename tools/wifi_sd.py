#!/usr/bin/env python3
"""Direct ESP3D SD transfer; never sends printer motion or print commands."""
import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import sys
from urllib.parse import quote, urlsplit

MAX_FILE = 128 * 1024 * 1024
MAX_RESPONSE = 1024 * 1024
TIMEOUT = 15
CHUNK = 64 * 1024
NAME = re.compile(r'[A-Z0-9_]{1,8}\.GCO\Z')


class TransferError(Exception):
    pass


def sd_name(filename):
    stem = re.sub(r'[^A-Z0-9]', '', os.path.splitext(os.path.basename(filename))[0].upper())
    return (stem[:4] or 'FILE') + hashlib.sha256(os.path.abspath(filename).encode()).hexdigest()[:4].upper() + '.GCO'


def valid_name(name):
    if not NAME.fullmatch(name):
        raise TransferError('SD filename must be uppercase 8.3, with a GCO extension')
    return name


class Client:
    def __init__(self, host, timeout=TIMEOUT):
        # Host and optional port only. HTTPConnection directly avoids proxy and redirects.
        parsed = urlsplit('http://' + host)
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise TransferError('invalid host or port') from exc
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path or
                parsed.query or parsed.fragment or not 1 <= port <= 65535):
            raise TransferError('host must be a hostname or IP with optional port, without a URL or path')
        self.host, self.port, self.timeout = parsed.hostname, port, timeout

    def connection(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, path):
        conn = self.connection()
        try:
            conn.request('GET', path, headers={'Connection': 'close'})
            response = conn.getresponse()
            if response.status != 200:
                raise TransferError(f'HTTP {response.status} for {path}')
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise TransferError('response exceeds 1 MiB limit')
            return data
        finally:
            conn.close()

    def command(self, value):
        return self.request('/command?plain=' + quote(value, safe='')).decode('utf-8', 'strict').strip()

    def probe(self):
        info = self.command('[ESP800]')
        if not re.search(r'(?:^|#)\s*FW HW:\s*Direct SD\s*(?:#|$)', info, re.I):
            raise TransferError('ESP800 did not report Direct SD capability')
        card = self.command('[ESP200]')
        if card != 'SD card detected':
            raise TransferError('SD card not ready: ' + card[:120])
        return info

    def listing(self):
        try:
            data = json.loads(self.request('/upload?path=%2F'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransferError('invalid SD listing JSON') from exc
        if not isinstance(data, dict) or data.get('status') != 'Ok' or data.get('mode') != 'direct' or data.get('path') != '/' or not isinstance(data.get('files'), list):
            raise TransferError('SD listing unavailable, busy, or not direct mode')
        for entry in data['files']:
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not isinstance(entry.get('shortname'), str):
                raise TransferError('invalid SD listing entry')
        return data['files']

    def upload(self, filename, name):
        name = valid_name(name)
        with open(filename, 'rb') as source:
            size = os.fstat(source.fileno()).st_size
            if not 0 < size <= MAX_FILE:
                raise TransferError('file size must be between 1 byte and 128 MiB')
            self.probe()
            entries = self.listing()
            if any(name.casefold() in (entry['name'].casefold(), entry['shortname'].casefold()) for entry in entries):
                raise TransferError('SD filename already exists; refusing overwrite')
            boundary = '----wifi-sd-' + secrets.token_hex(16)
            def field(key, value):
                return (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode('ascii')
            prefix = (field('path', '/') + field('/' + name + 'S', str(size)) +
                      f'--{boundary}\r\nContent-Disposition: form-data; name="myfile[]"; filename="/{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode('ascii'))
            suffix = f'\r\n--{boundary}--\r\n'.encode('ascii')
            conn = self.connection()
            digest = hashlib.sha256()
            try:
                conn.putrequest('POST', '/upload')
                conn.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
                conn.putheader('Content-Length', str(len(prefix) + size + len(suffix)))
                conn.putheader('Connection', 'close')
                conn.endheaders()
                conn.send(prefix)
                remaining = size
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise TransferError('local file shrank during upload; remote file may be incomplete')
                    digest.update(chunk)
                    conn.send(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise TransferError('local file grew during upload; remote file may be incomplete')
                conn.send(suffix)
                response = conn.getresponse()
                if response.status != 200:
                    raise TransferError(f'upload HTTP {response.status}; remote file may be incomplete')
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise TransferError('upload response exceeds 1 MiB')
                try:
                    result = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise TransferError('invalid upload response; remote file may be incomplete') from exc
                if not isinstance(result, dict) or result.get('status') != 'Ok' or result.get('mode') != 'direct':
                    raise TransferError('upload rejected or failed; remote file may be incomplete')
            finally:
                conn.close()
        self.verify(name, size, digest.digest())
        return digest.hexdigest()

    def verify(self, name, size, expected):
        conn = self.connection()
        digest, total = hashlib.sha256(), 0
        try:
            conn.request('GET', '/SD/' + name, headers={'Connection': 'close'})
            response = conn.getresponse()
            if response.status != 200:
                raise TransferError(f'verification HTTP {response.status}')
            while True:
                data = response.read(min(CHUNK, size + 1 - total))
                if not data:
                    break
                total += len(data)
                if total > size:
                    raise TransferError('verification size mismatch')
                digest.update(data)
            if total != size or digest.digest() != expected:
                raise TransferError('verification size or SHA256 mismatch')
        finally:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Direct Wi-Fi transfer to the printer SD card')
    parser.add_argument('--host', required=True, help='printer hostname or IP, optional :port')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('probe')
    commands.add_parser('list')
    upload = commands.add_parser('upload')
    upload.add_argument('file')
    upload.add_argument('--name', help='uppercase 8.3 GCO filename')
    args = parser.parse_args(argv)
    try:
        client = Client(args.host)
        if args.action == 'probe':
            print(client.probe())
            print('SD listing:', len(client.listing()), 'entries')
        elif args.action == 'list':
            client.probe()
            print(json.dumps(client.listing(), indent=2))
        else:
            name = valid_name(args.name or sd_name(args.file))
            digest = client.upload(args.file, name)
            print(f'Uploaded and verified {name}: SHA256 {digest}')
        return 0
    except (TransferError, OSError, UnicodeError, http.client.HTTPException) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
