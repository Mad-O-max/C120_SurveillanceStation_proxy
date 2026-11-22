#!/usr/bin/env python3
"""Loopback ONVIF and RTSP proxy for the Tapo C120, Python 3.8+."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import logging
import http.client
import os
import re
import select
import signal
import socket
import socketserver
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from proxy_settings import DEFAULTS, configure_logging, credentials, settings

VERSION = '3.2.0'
LOG = logging.getLogger('c120')
SAFE_OPERATIONS = {'CreatePullPointSubscription', 'GetProfiles', 'GetCapabilities',
                   'GetServices', 'GetStreamUri', 'GetEventProperties'}
MAX_SUBSCRIPTIONS = 4
MAX_ROUTES = 64

EVENT_OPERATIONS = {'PullMessages', 'Renew', 'Unsubscribe', 'SetSynchronizationPoint', 'Seek'}
SAFE_OPERATIONS |= EVENT_OPERATIONS


class RecoveryBackoff(ValueError):
    pass


def log(message, level=logging.INFO):
    LOG.log(level, message)


def route_path(path):
    # Reject encoded delimiters, traversal, and non-ONVIF targets.
    return re.fullmatch(
        r'/(?:onvif/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*|event-[A-Za-z0-9_-]+)(?:\?[A-Za-z0-9_.=&%-]+)?',
        path,
    ) is not None


def camera_endpoint(camera, address):
    url = urlsplit(address)
    path = url.path + (('?' + url.query) if url.query else '')
    if (url.scheme != 'http' or url.hostname != camera or url.username is not None
            or url.fragment or url.port == 0 or not route_path(path)
            or any(ord(char) < 33 for char in address)):
        raise ValueError('Invalid camera endpoint')
    return url, path


class CameraAuthError(ValueError):
    pass


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def xml_text(root, name):
    return next(((el.text or '').strip() for el in root.iter()
                 if local_name(el.tag) == name), '')


def fault_name(payload):
    try:
        root = ET.fromstring(payload)
        fault = next((el for el in root.iter() if local_name(el.tag) == 'Fault'), None)
        if fault is None:
            return ''
        values = [(el.text or '').strip() for el in fault.iter()
                  if local_name(el.tag) == 'Value']
        # Only fault-code QNames, never arbitrary error text containing secrets.
        code = values[-1] if values else 'Fault'
        return code.split(':')[-1] if re.fullmatch(r'(?:[A-Za-z0-9_.-]+:)?(?:Sender|Receiver|NotAuthorized|FailedAuthentication|ResourceUnknown|InvalidArgVal|ActionNotSupported|InvalidSecurity|UnableToRenew|MessageExpired|Fault)', code) else 'Fault'
    except ET.ParseError:
        return ''


def duration(body, tag, seconds):
    """Replace an element including xsi:nil; preserve its namespace prefix."""
    tag_bytes = tag.encode()
    pattern = (rb'<(?P<q>(?:[\w.-]+:)?' + tag_bytes +
               rb')\b[^>]*(?:/\s*>|>[^<]*</(?P=q)\s*>)')
    def repl(m):
        return b'<' + m['q'] + b'>PT' + str(seconds).encode() + b'S</' + m['q'] + b'>'
    return re.sub(pattern, repl, body)


def exchange(camera, url, body, headers=None, timeout=10):
    u, path = camera_endpoint(camera, url)
    h = dict(headers or {'Content-Type': 'application/soap+xml; charset=utf-8'})
    h['Host'] = '{}:{}'.format(camera, u.port or 80)
    h['Accept-Encoding'] = 'identity'
    h['Connection'] = 'close'
    conn = http.client.HTTPConnection(camera, u.port or 80, timeout=timeout)
    timer = None
    try:
        deadline = time.monotonic() + timeout
        conn.connect()
        peer = conn.sock
        timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt_socket, (peer,))
        timer.daemon = True
        timer.start()
        conn.request('POST', path, body=body, headers=h)
        resp = conn.getresponse()
        payload = resp.read(MAX_BODY + 1)
        if len(payload) > MAX_BODY:
            raise ValueError('Response too large')
        if b'\x00' in payload or re.search(rb'<!\s*(?:DOCTYPE|ENTITY)', payload, re.I):
            raise ValueError('Unsupported upstream SOAP encoding or DTD')
        if resp.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed response unsupported')
        return resp.status, resp.getheaders(), payload
    finally:
        if timer:
            timer.cancel()
        conn.close()


class Subscription:
    def __init__(self, alias, url, create_url, template):
        self.alias, self.url = alias, url
        self.create_url, self.template = create_url, template
        self.lock = threading.RLock()
        self.deadline = 0
        self.margin = 20
        self.offset = 0
        self.refs = ''
        self.failed = False
        self.retry_at = 0
        self.failures = 0
        self.active = True


class Subscriptions:
    """One maintained upstream subscription per client subscription path."""
    def __init__(self, routing, username, password, lifetime=600, transport=None,
                 renew_margin=30, pull_timeout=5, io_timeout=8):
        self.routing, self.username, self.password = routing, username, password
        self.lifetime = lifetime
        self.renew_margin = renew_margin
        self.pull_timeout, self.io_timeout = pull_timeout, io_timeout
        self.send = transport or (lambda url, body, headers=None, timeout=10:
                                 exchange(routing.camera, url, body, headers, timeout))
        self.items = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.maintenance = {}
        self.create_slot = threading.Lock()

    def get(self, path):
        with self.lock:
            return self.items.get(path)

    def update_times(self, sub, payload, references=False, new_lease=True):
        root = ET.fromstring(payload)
        current, end = xml_text(root, 'CurrentTime'), xml_text(root, 'TerminationTime')
        if not current or not end:
            raise ValueError('Missing subscription times')
        current_dt = datetime.fromisoformat(current.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        ttl = (end_dt - current_dt).total_seconds()
        if ttl <= 0:
            raise ValueError('Expired subscription')
        if new_lease or not sub.deadline:
            # Anchor device UTC to our monotonic clock once per granted lease.
            # Pull responses can report the time at request start (several seconds old).
            sub.deadline = time.monotonic() + ttl
            sub.margin = min(max(1, ttl / 2),
                             max(self.io_timeout + 2, min(self.renew_margin, ttl / 3)))
            sub.offset = current_dt.timestamp() - time.time()
        if references:
            sub.refs = ''
            for el in root.iter():
                if local_name(el.tag) == 'ReferenceParameters':
                    parts = []
                    for child in el:
                        child.set('{http://www.w3.org/2005/08/addressing}IsReferenceParameter', 'true')
                        parts.append(ET.tostring(child, encoding='unicode'))
                    sub.refs = ''.join(parts)
        return ttl

    def endpoint(self, payload):
        root = ET.fromstring(payload)
        for el in root.iter():
            if local_name(el.tag) == 'SubscriptionReference':
                url = xml_text(el, 'Address')
                camera_endpoint(self.routing.camera, url)
                return url
        raise ValueError('Missing subscription reference')

    def register(self, payload, create_url, template, alias_override=None):
        url = self.endpoint(payload)
        u = urlsplit(url)
        alias = alias_override or (u.path + (('?' + u.query) if u.query else ''))
        sub = Subscription(alias, url, create_url, template)
        ttl = self.update_times(sub, payload, references=True)
        with self.lock:
            old = self.items.get(alias)
            if old:
                old.active = False
            if not old and len(self.items) >= MAX_SUBSCRIPTIONS:
                raise RecoveryBackoff('Subscription capacity reached')
            self.items[alias] = sub
        if not alias.startswith('/onvif/'):
            with self.routing.lock:
                self.routing.remember(alias, (u.port or 80, url))
        log('Subscription created camera:{} granted={:.0f}s renew-before={:.0f}s'.format(
            u.port or 80, ttl, sub.margin))
        return sub

    def bootstrap(self, path):
        # After proxy restart, SS may resume an old subscription without creating
        # one. Create a fresh owned lease so auto-renew is active immediately.
        with self.create_slot:
            existing = self.get(path)
            if existing:
                return existing
            create_url = self.routing.destination('/onvif/service')[1]
            template = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                        b'<s:Body><CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
                        b'<InitialTerminationTime>PT600S</InitialTerminationTime>'
                        b'</CreatePullPointSubscription></s:Body></s:Envelope>')
            temp = Subscription(path, create_url, create_url, template)
            temp.failed = True
            with self.lock:
                if len(self.items) >= MAX_SUBSCRIPTIONS:
                    raise RecoveryBackoff('Subscription capacity reached')
                self.items[path] = temp
            with temp.lock:
                self.ensure(temp)
            if not path.startswith('/onvif/'):
                with self.routing.lock:
                    self.routing.remember(path, (urlsplit(temp.url).port or 80, temp.url))
            log('Resumed client subscription adopted; auto-renew active')
            return temp

    def header(self, sub, action, url, refs=True):
        nonce = os.urandom(20)
        created = datetime.fromtimestamp(time.time() + sub.offset, timezone.utc).isoformat(
            timespec='seconds').replace('+00:00', 'Z')
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode() + self.password.encode()).digest()).decode()
        return '''<s:Header xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://www.w3.org/2005/08/addressing"
 xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
 xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
 <a:Action>{action}</a:Action><a:MessageID>urn:uuid:{msgid}</a:MessageID>
 <a:To>{url}</a:To><a:ReplyTo><a:Address>http://www.w3.org/2005/08/addressing/anonymous</a:Address></a:ReplyTo>
 <wsse:Security><wsse:UsernameToken><wsse:Username>{user}</wsse:Username>
 <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
 <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</wsse:Nonce>
 <wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>{refs}</s:Header>'''.format(
            action=escape(action), msgid=uuid.uuid4(), url=escape(url), user=escape(self.username),
            digest=digest, nonce=base64.b64encode(nonce).decode(), created=created,
            refs=sub.refs if refs else '').encode()

    def fresh_header(self, sub, body, action, url, refs=True):
        header = self.header(sub, action, url, refs)
        pattern = rb'<(?:[\w.-]+:)?Header\b[^>]*(?:/\s*>|>.*?</(?:[\w.-]+:)?Header\s*>)'
        if re.search(pattern, body, re.S):
            return re.sub(pattern, lambda m: header, body, count=1, flags=re.S)
        return re.sub(rb'(<(?:[\w.-]+:)?Envelope\b[^>]*>)',
                      lambda m: m[1] + header, body, count=1)

    def renew(self, sub):
        action = 'http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest'
        body = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
                self.header(sub, action, sub.url) +
                ('<s:Body><Renew xmlns="http://docs.oasis-open.org/wsn/b-2">'
                 '<TerminationTime>PT{}S</TerminationTime></Renew></s:Body></s:Envelope>').format(
                     self.lifetime).encode())
        status, _, payload = self.send(sub.url, body, timeout=self.io_timeout)
        fault = fault_name(payload)
        if status in (401, 403) or fault.split(':')[-1] in (
                'NotAuthorized', 'FailedAuthentication', 'InvalidSecurity'):
            raise CameraAuthError('Camera authentication rejected')
        if status != 200 or fault:
            log('Renew -> camera:{} HTTP {} fault={}'.format(
                urlsplit(sub.url).port or 80, status, fault or '-'), logging.WARNING)
            raise ValueError('Renew HTTP {} fault={}'.format(status, fault or 'none'))
        ttl = self.update_times(sub, payload)
        log('Subscription renewed camera:{} granted={:.0f}s'.format(urlsplit(sub.url).port or 80, ttl))

    def recover(self, sub):
        # Retain original filters and namespace declarations, replace stale WSSE.
        action = 'http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest'
        body = duration(sub.template, 'InitialTerminationTime', self.lifetime)
        body = self.fresh_header(sub, body, action, sub.create_url, refs=False)
        status, _, payload = self.send(sub.create_url, body, timeout=self.io_timeout)
        fault = fault_name(payload)
        if status in (401, 403) or fault.split(':')[-1] in (
                'NotAuthorized', 'FailedAuthentication', 'InvalidSecurity'):
            raise CameraAuthError('Camera authentication rejected')
        if status != 200 or fault:
            log('Recreate -> camera:{} HTTP {} fault={}'.format(
                urlsplit(sub.create_url).port or 80, status, fault or '-'), logging.WARNING)
            raise ValueError('Recreate HTTP {} fault={}'.format(status, fault or 'none'))
        new_url = self.endpoint(payload)
        ttl = self.update_times(sub, payload, references=True)
        sub.url = new_url
        sub.failed = False
        log('Subscription recovered camera:{} granted={:.0f}s; client path retained'.format(
            urlsplit(new_url).port or 80, ttl))

    def defer(self, sub, exc, operation):
        sub.failed = True
        sub.failures += 1
        delay = 300 if isinstance(exc, CameraAuthError) else min(60, 5 * 2 ** min(sub.failures - 1, 4))
        sub.retry_at = time.monotonic() + delay
        log('{} camera:{} {}; retry in {}s'.format(
            operation, urlsplit(sub.url).port or 80, type(exc).__name__, delay), logging.WARNING)

    def ensure(self, sub):
        if not sub.active or self.stop.is_set():
            raise RecoveryBackoff('Subscription inactive')
        now = time.monotonic()
        if now < sub.retry_at:
            raise RecoveryBackoff('Subscription recovery backoff')
        if not sub.failed and now < sub.deadline - sub.margin:
            return
        try:
            if sub.failed or now >= sub.deadline:
                self.recover(sub)
            else:
                try:
                    self.renew(sub)
                except CameraAuthError:
                    raise
                except (OSError, ValueError, http.client.HTTPException, ET.ParseError) as exc:
                    log('Renew camera:{} failed {}; recreating subscription'.format(
                        urlsplit(sub.url).port or 80, type(exc).__name__), logging.WARNING)
                    self.recover(sub)
            sub.failures, sub.retry_at = 0, 0
        except (OSError, ValueError, http.client.HTTPException, ET.ParseError) as exc:
            self.defer(sub, exc, 'Subscription maintenance')
            raise

    def close(self):
        # Share a small shutdown budget across all leases.
        deadline = time.monotonic() + 3
        with self.lock:
            items = list(self.items.values())
        for sub in items:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not sub.lock.acquire(blocking=False):
                continue
            try:
                if not sub.active or sub.deadline <= time.monotonic():
                    continue
                action = 'http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest'
                body = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
                        self.header(sub, action, sub.url) +
                        b'<s:Body><Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>'
                        b'</s:Body></s:Envelope>')
                self.send(sub.url, body, timeout=min(1, remaining))
            except (OSError, ValueError, http.client.HTTPException):
                log('Shutdown Unsubscribe failed', logging.DEBUG)
            finally:
                sub.lock.release()

    def maintain(self, sub):
        if not sub.lock.acquire(blocking=False):
            return
        try:
            self.ensure(sub)
        except (OSError, ValueError, http.client.HTTPException, ET.ParseError):
            pass
        finally:
            sub.lock.release()

    def run(self):
        while not self.stop.wait(1):
            with self.lock:
                items = list(self.items.values())
            self.maintenance = {sub: job for sub, job in self.maintenance.items() if job.is_alive()}
            now = time.monotonic()
            for sub in items:
                due = sub.failed or now >= sub.deadline - sub.margin
                if (not due or now < sub.retry_at or not sub.active
                        or sub in self.maintenance or len(self.maintenance) >= MAX_SUBSCRIPTIONS):
                    continue
                job = threading.Thread(target=self.maintain, args=(sub,), daemon=True)
                self.maintenance[sub] = job
                job.start()

    def forward(self, path, body, headers):
        operation, _ = soap_summary(body)
        sub = self.get(path) if operation in EVENT_OPERATIONS else None
        if sub is None and operation == 'PullMessages':
            sub = self.bootstrap(path)
        if sub is None:
            route = self.routing.destination(path)
            if route is None:
                raise ValueError('Unknown subscription path')
            _, url = route
            if operation != 'CreatePullPointSubscription':
                status, headers, payload = self.send(url, self.routing.request(body, url),
                                                     headers, timeout=self.io_timeout)
                return status, headers, self.routing.response(payload), url
            with self.create_slot:
                with self.lock:
                    if len(self.items) >= MAX_SUBSCRIPTIONS:
                        raise RecoveryBackoff('Subscription capacity reached')
                body = duration(body, 'InitialTerminationTime', self.lifetime)
                body = self.routing.request(body, url)
                status, response_headers, payload = self.send(url, body, headers,
                                                               timeout=self.io_timeout)
                if status == 200 and not fault_name(payload):
                    self.register(payload, url, body)
                return status, response_headers, self.routing.response(payload), url
        # Do not queue a second pull against the same lease. A client retry can
        # proceed after the in-flight request completes.
        if not sub.lock.acquire(blocking=False):
            raise RecoveryBackoff('Subscription busy')
        try:
            if operation != 'Unsubscribe':
                self.ensure(sub)
            # Refresh WS-Security so retries/recovery never replay old nonces.
            root = ET.fromstring(body)
            action = xml_text(root, 'Action')
            if not action:
                # SS sometimes resumes pulls through /onvif/service without WSA.
                if operation in ('Renew', 'Unsubscribe'):
                    action = 'http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/{}Request'.format(operation)
                elif operation in EVENT_OPERATIONS:
                    action = 'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/{}Request'.format(operation)
                else:
                    raise ValueError('Missing SOAP action')
            body = self.fresh_header(sub, body, action, sub.url)
            if operation == 'PullMessages':
                # Bound a blocking pull so the renew worker cannot be starved.
                match = re.search(rb'<(?:[\w.-]+:)?Timeout\b[^>]*>([^<]*)<', body)
                valid = re.fullmatch(rb'PT([0-9]{1,6}(?:\.[0-9]{1,6})?)S', match[1].strip()) if match else None
                if not valid or float(valid[1]) > self.pull_timeout:
                    capped = duration(body, 'Timeout', self.pull_timeout)
                    if capped == body:
                        raise ValueError('Missing PullMessages timeout')
                    body = capped
            used_url = sub.url
            try:
                result = self.send(used_url, body, None, timeout=self.io_timeout)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # A lost response may already have consumed events.
                self.defer(sub, exc, operation)
                raise
            status, response_headers, payload = result
            fault = fault_name(payload)
            if operation == 'Unsubscribe' and status == 200 and not fault:
                sub.active = False
                with self.lock:
                    if self.items.get(path) is sub:
                        del self.items[path]
            elif status != 200 or fault:
                error = (CameraAuthError() if status in (401, 403) or fault.split(':')[-1] in
                         ('NotAuthorized', 'FailedAuthentication', 'InvalidSecurity') else ValueError())
                self.defer(sub, error, operation)
            elif operation == 'Renew':
                try:
                    self.update_times(sub, payload)
                except (ValueError, ET.ParseError) as exc:
                    self.defer(sub, exc, operation)
                    raise
            if operation == 'PullMessages':
                return status, response_headers, payload, used_url
            new_public = self.routing.origin + urlsplit(sub.url).path
            if urlsplit(sub.url).query:
                new_public += '?' + urlsplit(sub.url).query
            payload = self.routing.response(payload)
            payload = rewrite_addresses(payload, {new_public: self.routing.origin + sub.alias})
            return status, response_headers, payload, used_url
        finally:
            sub.lock.release()

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length'}
MAX_BODY = 4 * 1024 * 1024


def interrupt_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class BoundedConnections:
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, *args, limit=8, timeout=8):
        self.slots = threading.BoundedSemaphore(limit)
        self.peers = set()
        self.peers_lock = threading.Lock()
        self.closing = False
        self.warnings = {}
        self.warning_lock = threading.Lock()
        self.io_timeout = timeout
        super().__init__(*args)

    def track(self, peer):
        with self.peers_lock:
            if self.closing:
                interrupt_socket(peer)
                return False
            self.peers.add(peer)
            return True

    def untrack(self, peer):
        with self.peers_lock:
            self.peers.discard(peer)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(self.io_timeout)
        self.track(request)
        try:
            super().process_request(request, address)
        except BaseException:
            self.untrack(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.untrack(request)
            self.slots.release()

    def warn(self, key, message):
        now = time.monotonic()
        with self.warning_lock:
            if now < self.warnings.get(key, 0):
                return
            if len(self.warnings) >= 64 and key not in self.warnings:
                del self.warnings[next(iter(self.warnings))]
            self.warnings[key] = now + 30
        log(message, logging.WARNING)

    def handle_error(self, request, address):
        log('Connection handler failed: {}'.format(type(sys.exc_info()[1]).__name__), logging.WARNING)

    def server_close(self):
        with self.peers_lock:
            self.closing = True
            for peer in self.peers:
                interrupt_socket(peer)
        super().server_close()


class RtspRelay(socketserver.BaseRequestHandler):
    """Raw TCP tunnel: preserve RTSP Digest URI and interleaved RTP/RTCP."""
    def handle(self):
        upstream = None
        side = 'camera connect'
        try:
            upstream = socket.create_connection(self.server.upstream, timeout=self.server.io_timeout)
            if not self.server.track(upstream):
                return
            upstream.settimeout(self.server.io_timeout)
            log('RTSP connected -> camera:{}'.format(self.server.upstream[1]))
            peers = {self.request: upstream, upstream: self.request}
            labels = {self.request: 'client', upstream: 'camera'}
            readers = list(peers)
            last_data = time.monotonic()
            while readers:
                side = 'select'
                ready, _, _ = select.select(readers, [], [], 1)
                if not ready and time.monotonic() - last_data >= 120:
                    break
                for src in ready:
                    side = labels[src] + ' read'
                    data = src.recv(65536)
                    if data:
                        last_data = time.monotonic()
                        side = labels[peers[src]] + ' write'
                        peers[src].sendall(data)
                    else:
                        readers.remove(src)
                        try:
                            peers[src].shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        except OSError as exc:
            if not self.server.closing:
                self.server.warn(side, 'RTSP camera:{} {}: {}'.format(
                    self.server.upstream[1], side, type(exc).__name__))
        finally:
            if upstream:
                self.server.untrack(upstream)
                upstream.close()
            log('RTSP connection closed', logging.DEBUG)


class RtspServer(BoundedConnections, socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class OnvifServer(BoundedConnections, ThreadingHTTPServer):
    pass


def soap_summary(body):
    """Log only operation name and event count, never credentials or XML."""
    try:
        root = ET.fromstring(body)
        operation = 'unknown'
        for el in root.iter():
            if el.tag.rsplit('}', 1)[-1] == 'Body' and len(el):
                operation = el[0].tag.rsplit('}', 1)[-1]
                break
        events = sum(el.tag.rsplit('}', 1)[-1] == 'NotificationMessage'
                     for el in root.iter())
        return operation, events
    except ET.ParseError:
        return 'non-XML', 0


def rewrite_addresses(body, replacements):
    if not replacements:
        return body
    urls = {escape(old).encode(): escape(new).encode() for old, new in replacements.items()}
    pattern = (rb'(<(?P<tag>(?:[\w.-]+:)?(?:Address|XAddr|Uri))\b[^>]*>)'
               rb'(\s*)([^<>]*?)(\s*)(</(?P=tag)\s*>)')
    return re.sub(pattern, lambda match: (
        match[1] + match[3] + urls.get(match[4], match[4]) + match[5] + match[6]), body)


class Routing:
    def __init__(self, camera, origin, camera_port=2020):
        self.camera = camera
        self.camera_port = camera_port
        self.origin = origin.rstrip('/')
        self.routes = {}
        self.lock = threading.Lock()

    def response(self, body):
        # Inspect URLs without serializing XML again: preserve namespace prefixes.
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return body
        replacements = {}
        with self.lock:
            for el in root.iter():
                if el.tag.rsplit('}', 1)[-1] not in ('Address', 'XAddr', 'Uri'):
                    continue
                raw = (el.text or '').strip()
                try:
                    url, path = camera_endpoint(self.camera, raw)
                except ValueError:
                    continue
                self.remember(path, (url.port or 80, raw))
                replacements[raw] = self.origin + path
        return rewrite_addresses(body, replacements)

    def remember(self, path, route):
        # Caller holds routing.lock.
        if len(self.routes) >= MAX_ROUTES and path not in self.routes:
            del self.routes[next(iter(self.routes))]
        self.routes[path] = route

    def destination(self, path):
        if not route_path(path):
            return None
        if path.partition('?')[0] == '/onvif/service':
            return self.camera_port, 'http://{}:{}{}'.format(self.camera, self.camera_port, path)
        with self.lock:
            route = self.routes.get(path)
        if route:
            return route
        if path.startswith('/onvif/'):
            return self.camera_port, 'http://{}:{}{}'.format(self.camera, self.camera_port, path)
        return None

    def request(self, body, original_url):
        # Change only WS-Addressing To, not credentials or event data.
        pattern = rb'(<(?:[\w.-]+:)?To\b[^>]*>)([^<]*)(</(?:[\w.-]+:)?To\s*>)'
        def replace(match):
            return match[1] + original_url.replace('&', '&amp;').encode() + match[3]
        return re.sub(pattern, replace, body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/healthz':
            self.reply(200, b'ONVIF proxy ready\n')
        else:
            self.reply(405, b'Use ONVIF SOAP POST\n')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        route = self.server.routing.destination(self.path)
        if route is None and self.path.startswith('/event-') and route_path(self.path):
            route = self.server.routing.destination('/onvif/service')
        if route is None:
            self.reply(404, b'Unknown subscription; reactivate camera to create one\n')
            return
        if self.headers.get('Transfer-Encoding'):
            self.reply(400, b'Chunked requests unsupported\n')
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.reply(413, b'Invalid request length\n')
            return
        operation, port = 'read-request', '?'
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete body')
            if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)', body, re.I) or b'\x00' in body:
                raise ValueError('Unsupported SOAP encoding or DTD')
            operation, _ = soap_summary(body)
            if operation not in SAFE_OPERATIONS:
                operation = 'other'
            port, _ = route
            dynamic_hop = set(x.strip().lower() for x in
                              self.headers.get('Connection', '').split(','))
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP | dynamic_hop | {'host', 'accept-encoding'}}
            sub = self.server.subscriptions.get(self.path)
            if sub:
                port = urlsplit(sub.url).port or 80
            status, response_headers, payload, used_url = self.server.subscriptions.forward(
                self.path, body, headers)
            port = urlsplit(used_url).port or 80
            # Log transport result only. SOAP faults can also accompany HTTP 200.
            fault = fault_name(payload)
            _, event_count = soap_summary(payload)
            message = 'ONVIF {} -> camera:{} HTTP {} SOAP-fault={} code={} events={}'.format(
                operation, port, status, bool(fault), fault or '-', event_count)
            if fault or status >= 400:
                self.server.warn((operation, port, status, fault), message)
            else:
                log(message, logging.DEBUG)
            self.send_response(status)
            connection = next((v for k, v in response_headers if k.lower() == 'connection'), '')
            response_hop = set(x.strip().lower() for x in connection.split(','))
            for k, v in response_headers:
                if k.lower() not in HOP | response_hop:
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
        except RecoveryBackoff:
            try:
                self.reply(503, b'Subscription busy or recovery pending; retry later\n')
            except OSError:
                pass
        except (OSError, ValueError, http.client.HTTPException, ET.ParseError) as exc:
            self.server.warn((operation, port, type(exc)), 'ONVIF {} -> camera:{} failed: {}'.format(
                operation, port, type(exc).__name__))
            try:
                self.reply(502, b'Camera request failed; see proxy log\n')
            except OSError:
                pass


def self_test():
    r = Routing('192.0.2.51', 'http://127.0.0.1:12020')
    xml = b'<r xmlns:a="urn:test"><a:Address>http://192.0.2.51:1025/event-1025_1025</a:Address><Uri>rtsp://192.0.2.51:554/stream1</Uri></r>'
    result = r.response(xml)
    assert b'http://127.0.0.1:12020/event-1025_1025' in result
    assert b'rtsp://192.0.2.51:554/stream1' in result
    assert r.destination('/event-1025_1025')[0] == 1025
    assert r.destination('/event-unknown') is None
    assert r.destination('/onvif/service')[0] == 2020
    req = b'<r xmlns:a="urn:test"><a:To mustUnderstand="1">http://127.0.0.1:12020/event-1025_1025</a:To><Password>unchanged</Password></r>'
    req = r.request(req, r.destination('/event-1025_1025')[1])
    assert b':1025/event-1025_1025</a:To>' in req
    assert b'<Password>unchanged</Password>' in req
    print('Routing and XML tests passed')
    test_rtsp_relay()
    test_subscriptions()
    test_timer_and_resume()


def test_rtsp_relay():
    # Real loopback sockets: authentication round-trip plus binary RTP bytes.
    first = b'DESCRIBE rtsp://127.0.0.1:12554/stream1 RTSP/1.0\r\nCSeq: 1\r\n\r\n'
    challenge = b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest realm="test", nonce="123"\r\n\r\n'
    authorized = b'DESCRIBE rtsp://127.0.0.1:12554/stream1 RTSP/1.0\r\nAuthorization: Digest uri="rtsp://127.0.0.1:12554/stream1", response="unchanged"\r\n\r\n'
    video = b'RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n' + b'$\x00\x00\x06\x00\xff\r\n$\x80'
    rtcp = b'$\x01\x00\x04\x81\xc9\x00\x01'

    def receive(sock, n):
        data = b''
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise AssertionError('Unexpected EOF')
            data += chunk
        return data

    class FakeCamera(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            assert receive(self.request, len(first)) == first
            self.request.sendall(challenge)
            assert receive(self.request, len(authorized)) == authorized
            # Split a binary frame across writes; relay must not parse it as text.
            for chunk in (video[:9], video[9:-3], video[-3:]):
                self.request.sendall(chunk)
            assert receive(self.request, len(rtcp)) == rtcp
            self.request.sendall(b'OK')

    camera = RtspServer(('127.0.0.1', 0), FakeCamera)
    relay = RtspServer(('127.0.0.1', 0), RtspRelay)
    relay.upstream = camera.server_address
    threads = [threading.Thread(target=s.serve_forever, daemon=True)
               for s in (camera, relay)]
    for thread in threads:
        thread.start()
    try:
        with socket.create_connection(relay.server_address, timeout=5) as client:
            client.sendall(first)
            assert receive(client, len(challenge)) == challenge
            client.sendall(authorized)
            assert receive(client, len(video)) == video
            client.sendall(rtcp)
            assert receive(client, 2) == b'OK'
    finally:
        for s in (relay, camera):
            s.shutdown()
            s.server_close()
        for thread in threads:
            thread.join(timeout=3)
    print('RTSP authentication and binary RTP/RTCP relay tests passed')


def test_subscriptions():
    routing = Routing('192.0.2.51', 'http://127.0.0.1:12020')
    calls = []
    state = {'port': 1024, 'creates': 0, 'refuse_pull': False, 'offline': False}

    def reply(operation, port=1024, ttl=60):
        now = datetime.now(timezone.utc)
        end = datetime.fromtimestamp(now.timestamp() + ttl, timezone.utc)
        ref = ('<SubscriptionReference><a:Address>http://192.0.2.51:{0}/event-{0}_{0}'
               '</a:Address></SubscriptionReference>').format(port) if operation == 'CreatePullPointSubscriptionResponse' else ''
        events = ('<NotificationMessage><Topic>PeopleDetector/People</Topic></NotificationMessage>'
                  if operation == 'PullMessagesResponse' else '')
        return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Body><{op}>'
                '{ref}<CurrentTime>{now}</CurrentTime><TerminationTime>{end}</TerminationTime>'
                '{events}</{op}></s:Body></s:Envelope>').format(
                    op=operation, ref=ref, now=now.isoformat(), end=end.isoformat(), events=events).encode()

    def fake_send(url, body, headers=None, timeout=10):
        op, _ = soap_summary(body)
        calls.append((op, url, body))
        root = ET.fromstring(body)
        if state['offline']:
            raise ConnectionRefusedError()
        if op in ('Renew', 'CreatePullPointSubscription') and len(calls) > 1:
            assert xml_text(root, 'Username') == 'test-user'
            nonce = base64.b64decode(xml_text(root, 'Nonce'))
            created = xml_text(root, 'Created')
            expected = base64.b64encode(hashlib.sha1(
                nonce + created.encode() + b'test-secret').digest()).decode()
            assert xml_text(root, 'Password') == expected
        if op == 'CreatePullPointSubscription':
            assert xml_text(root, 'InitialTerminationTime') == 'PT600S'
            # QName text in Filter still resolves after regenerating the header.
            assert b'xmlns:tns1="urn:topics"' in body
            assert b'tns1:RuleEngine/PeopleDetector/People' in body
            state['creates'] += 1
            if state['creates'] > 1:
                state['port'] = 1025
            return 200, [], reply('CreatePullPointSubscriptionResponse', state['port'])
        assert ':{}'.format(state['port']) in url
        assert xml_text(root, 'To') == url
        if op == 'Renew':
            return 200, [], reply('RenewResponse', ttl=60)
        if op == 'PullMessages':
            if state['refuse_pull']:
                state['refuse_pull'] = False
                raise ConnectionRefusedError()
            assert xml_text(root, 'Timeout') == 'PT5S'
            return 200, [], reply('PullMessagesResponse')
        if op == 'Unsubscribe':
            return 200, [], reply('UnsubscribeResponse')
        raise AssertionError(op)

    manager = Subscriptions(routing, 'test-user', 'test-secret', transport=fake_send)
    create = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
              b'xmlns:tns1="urn:topics" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
              b'<s:Body><CreatePullPointSubscription><Filter>tns1:RuleEngine/PeopleDetector/People</Filter>'
              b'<InitialTerminationTime xsi:nil="true" /></CreatePullPointSubscription></s:Body></s:Envelope>')
    status, _, data, _ = manager.forward('/onvif/service', create, {})
    assert status == 200 and b'http://127.0.0.1:12020/event-1024_1024' in data
    sub = manager.get('/event-1024_1024')
    assert 55 < sub.deadline - time.monotonic() <= 60
    # Trigger renewal before the deadline, with no client Renew request.
    sub.deadline = time.monotonic() + 5
    with sub.lock:
        manager.ensure(sub)
    assert calls[-1][0] == 'Renew' and state['creates'] == 1
    # Expiration recreates the subscription on another port, retaining client URL.
    sub.deadline = time.monotonic() - 1
    with sub.lock:
        manager.ensure(sub)
    assert ':1025/' in sub.url and sub.alias == '/event-1024_1024'
    pull = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            b'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Header>'
            b'<a:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest</a:Action>'
            b'</s:Header><s:Body><PullMessages><Timeout>PT60S</Timeout><MessageLimit>8</MessageLimit>'
            b'</PullMessages></s:Body></s:Envelope>')
    status, _, data, url = manager.forward(sub.alias, pull, {})
    assert status == 200 and ':1025/' in url and soap_summary(data)[1] == 1
    state['refuse_pull'] = True
    before = len(calls)
    try:
        manager.forward(sub.alias, pull, {})
        raise AssertionError('Expected failed connection')
    except ConnectionRefusedError:
        assert sub.failed and len(calls) == before + 1  # Never replay a lost pull.
    # After backoff the next request recreates and pulls from the new endpoint.
    sub.retry_at = 0
    manager.forward(sub.alias, pull, {})
    assert not sub.failed and state['creates'] == 3
    state['offline'] = True
    sub.failed = True
    with sub.lock:
        try:
            manager.ensure(sub)
        except ConnectionRefusedError:
            pass
        before = len(calls)
        try:
            manager.ensure(sub)
        except ValueError:
            pass
        assert len(calls) == before  # Backoff prevents a request storm.
    # Verify the background worker renews without an incoming request.
    state['offline'] = False
    sub.failed, sub.retry_at = False, 0
    sub.deadline = time.monotonic() + 5
    original_send = manager.send
    def send_and_stop(*args, **kwargs):
        result = original_send(*args, **kwargs)
        manager.stop.set()
        return result
    manager.send = send_and_stop
    worker = threading.Thread(target=manager.run, daemon=True)
    worker.start()
    worker.join(timeout=4)
    assert not worker.is_alive() and calls[-1][0] == 'Renew'
    # End-to-end HTTP handler regression: event response reaches local client.
    manager.send = original_send
    manager.stop.clear()
    server = OnvifServer(('127.0.0.1', 0), Handler)
    server.routing, server.subscriptions = routing, manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        conn.request('POST', sub.alias, body=pull,
                     headers={'Content-Type': 'application/soap+xml'})
        response = conn.getresponse()
        payload = response.read()
        assert response.status == 200 and soap_summary(payload)[1] == 1
        assert int(response.getheader('Content-Length')) == len(payload)
        assert 'test-secret' not in payload.decode()
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    print('Subscription renewal, expiry, port-change recovery, WSSE and backoff tests passed')


def test_timer_and_resume():
    from unittest.mock import patch
    clock = [0.0]
    epoch = 1795377600.0
    actions = []

    def response(op, current=None, end=600):
        def stamp(seconds):
            return datetime.fromtimestamp(epoch + seconds, timezone.utc).isoformat()
        extra = ('<SubscriptionReference xmlns:a="http://www.w3.org/2005/08/addressing">'
                 '<a:Address>http://192.0.2.51:1024/event-1024_1024</a:Address>'
                 '</SubscriptionReference>') if op == 'CreatePullPointSubscriptionResponse' else ''
        return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                '<{op}>{extra}<CurrentTime>{now}</CurrentTime><TerminationTime>{end}</TerminationTime>'
                '</{op}></s:Body></s:Envelope>').format(
                    op=op, extra=extra, now=stamp(clock[0] if current is None else current),
                    end=stamp(end)).encode()

    def transport(url, body, headers=None, timeout=10):
        op, _ = soap_summary(body)
        actions.append((op, url))
        return 200, [], response(op + 'Response', end=clock[0] + 600 if op in (
            'Renew', 'CreatePullPointSubscription') else 600)

    with patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
         patch.object(time, 'time', side_effect=lambda: epoch + clock[0]):
        routing = Routing('192.0.2.51', 'http://127.0.0.1:12020')
        manager = Subscriptions(routing, 'user', 'secret', transport=transport)
        sub = manager.register(response('CreatePullPointSubscriptionResponse'),
                               'http://192.0.2.51:2020/onvif/service', b'<unused/>')
        assert sub.deadline == 600 and sub.margin == 30
        for elapsed in (3, 150, 300, 550, 569):
            clock[0] = elapsed
            # Match the capture: CurrentTime lags by ~4s, fixed TerminationTime.
            manager.update_times(sub, response('PullMessagesResponse', current=elapsed-4), new_lease=False)
            assert sub.deadline == 600 and sub.margin == 30
        clock[0] = 570
        with sub.lock:
            manager.ensure(sub)
        assert actions[-1][0] == 'Renew' and sub.deadline == 1170
        assert sub.margin == 30
        for elapsed in (600, 1000, 1139):
            clock[0] = elapsed
            manager.update_times(sub, response('PullMessagesResponse', current=elapsed-4, end=1170),
                                 new_lease=False)
            assert sub.deadline == 1170 and sub.margin == 30
        clock[0] = 1140
        with sub.lock:
            manager.ensure(sub)
        assert actions[-1][0] == 'Renew' and sub.deadline == 1740
        # Restart scenario seen in capture: SS resumes PullMessages on /onvif/service.
        clock[0] = 0
        routing = Routing('192.0.2.51', 'http://127.0.0.1:12020')
        manager = Subscriptions(routing, 'user', 'secret', transport=transport)
        pull = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                b'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Header>'
                b'<a:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest</a:Action>'
                b'</s:Header><s:Body><PullMessages><Timeout>PT2S</Timeout><MessageLimit>8</MessageLimit>'
                b'</PullMessages></s:Body></s:Envelope>')
        before = len(actions)
        pull = re.sub(rb'<a:Action>.*?</a:Action>', b'', pull)
        manager.forward('/onvif/service', pull, {})
        assert actions[before][0] == 'CreatePullPointSubscription'
        assert actions[-1] == ('PullMessages', 'http://192.0.2.51:1024/event-1024_1024')
        assert manager.get('/onvif/service').deadline == 600
        get_profiles = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                        b'<s:Body><GetProfiles/></s:Body></s:Envelope>')
        manager.forward('/onvif/service', get_profiles, {})
        assert actions[-1] == ('GetProfiles', 'http://192.0.2.51:2020/onvif/service')
    print('Fixed renewal margin, delayed camera clock and resumed-client tests passed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--credentials', help='Private JSON file; omit for terminal prompt')
    parser.add_argument('--log-file', help='Rotating log file')
    parser.add_argument('--check', action='store_true', help='Validate unattended settings')
    parser.add_argument('--diagnose', action='store_true', help='Print settings with camera address omitted')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--camera', help='Camera IPv4 address')
    for key in DEFAULTS:
        parser.add_argument('--' + key.replace('_', '-'), type=str if key == 'log_level' else int)
    args = parser.parse_args()
    if args.self_test:
        configure_logging('INFO')
        self_test()
        return
    try:
        cfg = settings(args)
        if args.diagnose:
            print('Proxy v{}; loopback-only'.format(VERSION))
            for name, value in sorted(vars(cfg).items()):
                if name != 'camera':
                    print('{}={}'.format(name, value))
            return
        if args.check and not args.credentials:
            raise ValueError('--check requires a credentials file')
        username, password = credentials(args.credentials)
        if args.check:
            print('Configuration and credential permissions valid')
            return
        configure_logging(cfg.log_level, args.log_file)
    except ValueError as exc:
        # Unicode errors can echo credential bytes; log only the type.
        parser.error('Invalid file encoding' if isinstance(exc, UnicodeError) else str(exc))
    except OSError as exc:
        parser.error('Cannot open settings, credentials or log ({})'.format(type(exc).__name__))

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    routing = Routing(cfg.camera, 'http://127.0.0.1:{}'.format(cfg.port), cfg.camera_http_port)
    manager = Subscriptions(routing, username, password, cfg.lifetime,
                            renew_margin=cfg.renew_margin, pull_timeout=cfg.pull_timeout,
                            io_timeout=cfg.io_timeout)
    try:
        with OnvifServer(('127.0.0.1', cfg.port), Handler, timeout=cfg.io_timeout) as server, \
             RtspServer(('127.0.0.1', cfg.rtsp_port), RtspRelay, limit=4, timeout=cfg.io_timeout) as rtsp:
            server.routing, server.subscriptions = routing, manager
            rtsp.upstream = (cfg.camera, cfg.camera_rtsp_port)
            threads = [threading.Thread(target=listener.serve_forever, daemon=True)
                       for listener in (server, rtsp)]
            worker = threading.Thread(target=manager.run, daemon=True)
            for thread in threads + [worker]:
                thread.start()
            log('Proxy v{} listening on loopback ONVIF:{} RTSP:{}; camera ports {}/{}'.format(
                VERSION, cfg.port, cfg.rtsp_port, cfg.camera_http_port, cfg.camera_rtsp_port))
            ready_fd = os.environ.get('C120_READY_FD')
            if ready_fd is not None:
                with os.fdopen(int(ready_fd), 'wb') as pipe:
                    pipe.write(b'1')
            while not stop.wait(0.5):
                if not all(thread.is_alive() for thread in threads + [worker]):
                    raise RuntimeError('Service thread exited')
            manager.stop.set()
            for listener in (server, rtsp):
                listener.shutdown()
                listener.server_close()
            worker.join(timeout=1)
            manager.close()
            log('Proxy stopped')
    except (OSError, RuntimeError) as exc:
        log('Proxy stopped after {} (errno {})'.format(
            type(exc).__name__, getattr(exc, 'errno', None)), logging.ERROR)
        sys.exit(1)


if __name__ == '__main__':
    main()
