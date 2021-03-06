#
# Copyright 2012-2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import errno
import os
import socket
import ssl
import tempfile
import threading

import pytest

from testlib import VdsmTestCase as TestCaseBase

from integration.sslhelper import get_server_socket, \
    KEY_FILE, CRT_FILE

from vdsm import utils
from vdsm.common import cmdutils
from vdsm.common import concurrent
from vdsm.common import commands
from vdsm.protocoldetector import MultiProtocolAcceptor
from vdsm.sslutils import CLIENT_PROTOCOL, SSLContext, SSLHandshakeDispatcher
from yajsonrpc.betterAsyncore import Reactor


class SSLServerThread(threading.Thread):
    """A very simple server thread.

    This server waits for SSL connections in a serial
    fashion and then echoes whatever the client sends.
    """

    def __init__(self, server):
        threading.Thread.__init__(self)
        self.server = server
        self.stop = threading.Event()

    def run(self):
        # It is important to set a timeout in the server thread to be
        # able to check periodically the stop flag:
        self.server.settimeout(1)

        # Accept client connections:
        while not self.stop.isSet():
            try:
                client, address = self.server.accept()
                client.settimeout(1)
                try:
                    while True:
                        data = client.recv(1024)
                        if data:
                            client.sendall(data)
                        else:
                            break
                except:
                    # We don't care about exceptions here, only on the
                    # client side:
                    pass
                finally:
                    client.close()
            except:
                # Nothing to do here, we will check the stop flag in the
                # next iteration of the loop:
                pass

    def shutdown(self):
        # Note that this doesn't stop the thready immediately, it just
        # indicates that stopping is requested, the thread will stop
        # with next iteration of the accept loop:
        self.stop.set()


class SSLTests(TestCaseBase):
    """Tests of SSL communication"""

    def setUp(self):
        """Prepares to run the tests.

        The preparation consist on creating temporary files containing
        the keys and certificates and starting a thread that runs a
        simple SSL server.
        """

        # Save the key to a file:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(KEY)
            self.keyfile = tmp.name

        # Save the certificate to a file:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(CERTIFICATE)
            self.certfile = tmp.name

        # Create the server socket:
        self.server = socket.socket()
        self.server = get_server_socket(self.keyfile, self.certfile,
                                        self.server)
        self.address = self.tryBind(ADDRESS)
        self.server.listen(5)

        # Start the server thread:
        self.thread = SSLServerThread(self.server)
        self.thread.deamon = True
        self.thread.start()

    def tryBind(self, address):
        ipadd, port = address
        while True:
            try:
                self.server.bind((ipadd, port))
                return (ipadd, port)
            except socket.error as ex:
                if ex.errno == errno.EADDRINUSE:
                    port += 1
                    if port > 65535:
                        raise socket.error(
                            errno.EADDRINUSE,
                            "Can not find available port to bind")
                else:
                    raise

    def tearDown(self):
        """Release the resources used by the tests.

        Removes the temporary files containing the keys and certifites,
        stops the server thread and closes the server socket.
        """

        # Delete the temporary files:
        os.remove(self.keyfile)
        os.remove(self.certfile)

        # Stop the server thread and wait for it to finish:
        self.thread.shutdown()
        self.thread.join()
        del self.thread

        # Close the server socket:
        self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
        del self.server

    def testConnectWithoutCertificateFails(self):
        """
        Verify that the connection without a client certificate
        fails.
        """

        with self.assertRaises(cmdutils.Error):
            args = ["openssl", "s_client", "-connect", "%s:%d" % self.address]
            commands.run(args)

    def testConnectWithCertificateSucceeds(self):
        """
        Verify that the connection with a valid client certificate
        works correctly.
        """

        args = ["openssl", "s_client", "-connect", "%s:%d" % self.address,
                "-cert", self.certfile, "-key", self.keyfile]
        commands.run(args)


@pytest.fixture
def fake_gethostbyaddr(monkeypatch, request):
    entry = getattr(request, 'param', None)
    if entry is not None:
        hostname, ipaddrlist = entry

        def impl(addr):
            if addr not in ipaddrlist:
                raise socket.herror()
            return (hostname, [], ipaddrlist)

        monkeypatch.setattr('vdsm.sslutils.socket.gethostbyaddr', impl)


@pytest.mark.parametrize('fake_gethostbyaddr', [('example.com', ['10.0.0.1'])],
                         indirect=True)
def test_same_string(fake_gethostbyaddr):
    assert SSLHandshakeDispatcher.compare_names('10.0.0.1', 'example.com')


@pytest.mark.parametrize('lhs,rhs', [('::ffff:127.0.0.1', '127.0.0.1'),
                                     ('127.0.0.1', '::ffff:127.0.0.1')])
def test_mapped_address(lhs, rhs):
    assert SSLHandshakeDispatcher.compare_names(lhs, rhs)


@pytest.mark.parametrize('fake_gethostbyaddr', [('example.com', ['10.0.0.1'])],
                         indirect=True)
def test_failed_mapped_address(fake_gethostbyaddr):
    assert not SSLHandshakeDispatcher.compare_names('10.0.0.1',
                                                    '::ffff:127.0.0.1')


@pytest.mark.parametrize('fake_gethostbyaddr',
                         [('example.com', ['10.0.0.1', '10.0.0.2'])],
                         indirect=True)
def test_multiple(fake_gethostbyaddr):
    assert SSLHandshakeDispatcher.compare_names('10.0.0.2', 'example.com')


@pytest.mark.parametrize('fake_gethostbyaddr',
                         [('evil.imposter.com', ['10.0.0.1'])],
                         indirect=True)
def test_imposter(fake_gethostbyaddr):
    assert not SSLHandshakeDispatcher.compare_names('10.0.0.1', 'example.com')


@pytest.mark.parametrize('lhs,rhs', [('127.0.0.1', 'example.com'),
                                     ('::1', 'example.com'),
                                     ('::ffff:127.0.0.1', 'example.com')])
def test_local_addresses(lhs, rhs):
    assert SSLHandshakeDispatcher.compare_names(lhs, rhs)


@pytest.fixture
def dummy_register_protocol_detector(monkeypatch):
    monkeypatch.setattr(MultiProtocolAcceptor, '_register_protocol_detector',
                        lambda d: d.close())


@pytest.fixture
def listener(dummy_register_protocol_detector, request):
    reactor = Reactor()

    excludes = getattr(request, 'param', 0)
    sslctx = SSLContext(cert_file=CRT_FILE, key_file=KEY_FILE,
                        ca_certs=CRT_FILE, excludes=excludes,
                        protocol=CLIENT_PROTOCOL)

    acceptor = MultiProtocolAcceptor(
        reactor,
        '127.0.0.1',
        0,
        sslctx=sslctx
    )

    try:
        t = concurrent.thread(reactor.process_requests)
        t.start()
        (host, port) = acceptor._acceptor.socket.getsockname()[0:2]
        yield (host, port)
    finally:
        acceptor.stop()
        t.join()


@pytest.fixture
def client_cmd(listener):

    def wrapper(protocol):
        (host, port) = listener
        cmd = ['openssl', 's_client', '-connect', '%s:%s' % (host, port),
               '-CAfile', CRT_FILE, '-cert', CRT_FILE, '-key', KEY_FILE,
               protocol]
        return commands.run(cmd)

    return wrapper


@pytest.mark.parametrize('protocol', ['-ssl2', '-ssl3'])
def test_tls_unsupported_protocols(client_cmd, protocol):
    with pytest.raises(cmdutils.Error):
        client_cmd(protocol)


@pytest.mark.parametrize('protocol', ['-tls1', '-tls1_1', '-tls1_2'])
def test_tls_protocols(client_cmd, protocol):
    assert b"Verify return code: 0 (ok)" in client_cmd(protocol)


@pytest.fixture
def use_client(listener):

    def wrapper(protocol):
        (host, port) = listener
        sslctx = SSLContext(cert_file=CRT_FILE, key_file=KEY_FILE,
                            ca_certs=CRT_FILE, protocol=protocol)
        return utils.create_connected_socket(host, port, sslctx=sslctx)

    return wrapper


def test_client_tlsv1(use_client):
    assert bool(use_client(ssl.PROTOCOL_SSLv23))


@pytest.mark.parametrize('listener',
                         [ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2],
                         indirect=True)
def test_client_tlsv12(use_client):
    with pytest.raises(ssl.SSLError) as e:
        use_client(ssl.PROTOCOL_TLSv1_2)

    # WRONG_VERSION_NUMBER
    assert e.value.errno == 1


# The address of the tests server:
ADDRESS = ("127.0.0.1", 8443)


# Private key used for the tests:
KEY = b"""
-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDapPcHwCWYsfiH
pJ/tXpcSZsa6ocJZaL3HF/mFxiO4/7za6lP0Vdtln4CwCzqAfUJKQhCHNyYUvZsf
Eylr0U30MQzhynq8+F5co5f2RNzz93aL7cjEUQMK2YaShLxz7o/QdoNSnT8sJ3TO
P16VEcpngoBD/nDXxNf0HekwhENYz4K2Hqol0xcGY6x8cJoXNybBPheVGTl6wy+r
W9YPuL0gR2/GgyVT1UP0EBGebkvza+eVaenrp0qrMiEQMDAOeNq3mu6ueOUo03Hn
xaEqxrToYv0eBbpF2Z469uJXaLP/NmcT1GUbFqP3H+/Js68HwxCEqb1kKGiG8E58
hSHHM95ZAgMBAAECggEAeMU2TmmsWrOze/lK/WqKN/fdPamsGIbqjNaJVYMkqENa
pfFZflUOYwu/oX4SSnbl7u6fApFLz5kL3hZPguaSEJgnbXDSax8lwDX88mMHSRsf
uBsYEphM/ek5lCUNk1vqxFMyJqgFBPamZmZKcDzreFF1WBlra0OnpYgADnSAXsT7
HcQDkSe1s1YuuRYYUuRc5KYhrQ5P3AHCJ++w7QK7wZbo/5iQuVuuytMBbCWFNH06
K+fEqZRB9wXg9ubvvbcAlX579QL2HRZl5GvhSP+2Jah/zoTndXAKVVWWx8L1ohKg
aAOxWGFy4f47BQwmkafZVYIGsfudEK4Dmf6UmwvVIQKBgQDw8r5ihTHuXLuyBtwy
J+Pn//zY1FKJcANshvFgQtrfbmLiulXDtvaiitdkQj8HyTeEtgtuGt5mnE5uKm8N
MV9eSU2FyuyazwlemI4XYdQWtcw+ZBh7K3u6/QjqDJfNjVDnv7S2VS9DDs8Ga7r4
fanecGfQ6ni5Mqxb2OAlOcBYRwKBgQDoTYmR35Lo/qkJ6Mm+8IljdvN3iAgqkO67
b6WhjkTwgO/Y+zGfQ/W2PbPsVWc1f3IBYvKmArvMDB5PZ9HyzIg27OxCyhjbLmvb
kEPjQF6f+FOb4h4yo9i2dBJucFAKrHMHiqH24Hlf3WOordxX9lY37M0fwpg2kZIM
ConIt/4EXwKBgDIXtV8UI+pTWy5K4NKImogsHywREEvEfuG8OEhz/b7/2w0aAiSb
UDFAvkD4yNPckG9FzaCJc31Pt7qNleLfRd17TeOn6YLR0jfZbYkM7KQADcNW2gQZ
aTLZ0lWeYpz4aT6VC4Pwt8+wL3g9Q3TP41X8dojnhkuybkT2FLuIgyWXAoGAMJUW
skU5qjSoEYR3vND9Sqnz3Qm7+3r4EocU8qaYUFwGzTArfo1t88EPwdtSjGOs6hFR
gdqMf+4A4MZrqAWSbzo5ZvZxIFWjBPY03G/32ijLA4zUl+6gQfggaqxecP0DyY36
tXDYsW3Ri9Ngg5znByck9wFxZ+glzRLfIfUo0K0CgYEAkogcGLKGb5zdwAXuUVQK
ftftLEARqs/gMA1cItxurtho0JUxYaaKgSICB7MQPEuTtdUNqCkeu9S838dbyfL7
gGdsZ26Can3IAyQv7+3DObvB376T4LD8Mp/ZHvOpeZQQ9O4ngadteRcBaCcd78Ij
VSgxeSvBewtCS1FnILwgXJ4=
-----END PRIVATE KEY-----
"""


# This is the certificate used for the tests, and it expires in Sep 26
# 2022, so don't be surprised if by that date the test starts failing:
CERTIFICATE = b"""
-----BEGIN CERTIFICATE-----
MIIC8zCCAdugAwIBAgIBADANBgkqhkiG9w0BAQUFADAUMRIwEAYDVQQDDAkxMjcu
MC4wLjEwHhcNMTIwOTI4MTcyMzE3WhcNMjIwOTI2MTcyMzE3WjAUMRIwEAYDVQQD
DAkxMjcuMC4wLjEwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDapPcH
wCWYsfiHpJ/tXpcSZsa6ocJZaL3HF/mFxiO4/7za6lP0Vdtln4CwCzqAfUJKQhCH
NyYUvZsfEylr0U30MQzhynq8+F5co5f2RNzz93aL7cjEUQMK2YaShLxz7o/QdoNS
nT8sJ3TOP16VEcpngoBD/nDXxNf0HekwhENYz4K2Hqol0xcGY6x8cJoXNybBPheV
GTl6wy+rW9YPuL0gR2/GgyVT1UP0EBGebkvza+eVaenrp0qrMiEQMDAOeNq3mu6u
eOUo03HnxaEqxrToYv0eBbpF2Z469uJXaLP/NmcT1GUbFqP3H+/Js68HwxCEqb1k
KGiG8E58hSHHM95ZAgMBAAGjUDBOMB0GA1UdDgQWBBR0dTG068xPsrXKDD6r6Ne+
8RQghzAfBgNVHSMEGDAWgBR0dTG068xPsrXKDD6r6Ne+8RQghzAMBgNVHRMEBTAD
AQH/MA0GCSqGSIb3DQEBBQUAA4IBAQCoY1bFkafDv3HIS5rBycVL0ghQV2ZgQzAj
sCZ47mgUVZKL9DiujRUFtzrMRhBBfyeT0Bv8zq+eijhGmjp8WqyRWDIwHoQwxHmD
EoQhAMR6pXvjZdYI/vwHJK5u0hADQZJ+zZp77m/p95Ds03l/g/FZHbCdISTTJnXw
t6oeDZzz/dQSAiuyAa6+0tdu2GNF8OkR5c7W+XmL797soiT1uYMgwIYQjM1NFkKN
vGc0b16ODiPvsB0bo+USw2M0grjsJEC0dN/GBgpFHO4oKAodvEWGGxANSHAXoD0E
bh5L7zBhjgag+o+ol2PDNZMrJlFvw8xzhQyvofx2h7H+mW0Uv6Yr
-----END CERTIFICATE-----
"""
