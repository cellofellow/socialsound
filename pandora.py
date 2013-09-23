import json
import sys
import time
import urllib
from cStringIO import StringIO

from twisted.internet import defer, task, reactor
from twisted.internet.ssl import ClientContextFactory
from twisted.python import failure, log
from twisted.web.client import Agent, readBody, FileBodyProducer
from twisted.web.client import ProxyAgent
from twisted.web.http_headers import Headers
from twisted.internet.endpoints import TCP4ClientEndpoint

from autobahn.websocket import (WebSocketServerFactory,
                                WebSocketServerProtocol,
                                listenWS)

from blowfish import Blowfish


class WebClientContextFactory(ClientContextFactory):
    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)


class PandoraError(IOError):
    def __init__(self, message, status=None, submsg=None):
        self.status = status
        self.message = message
        self.submsg = submsg


class PandoraAuthTokenInvalid(PandoraError):
    def __init__(self, message, submsg):
        self.status = API_ERROR.INVALID_AUTH_TOKEN
        self.message = message
        self.submsg = submsg


class PandoraNetError(PandoraError):
    pass


class PandoraAPIVersionError(PandoraError):
    def __init__(self, message, submsg):
        self.status = API_ERROR.API_VERSION_NOT_SUPPORTED
        self.message = message
        self.submsg = submsg


class PandoraTimeout(PandoraNetError):
    pass


class API_ERROR:
    API_VERSION_NOT_SUPPORTED = 11
    COUNTRY_NOT_SUPPORTED = 12
    INSUFFICIENT_CONNECTIVITY = 13
    READ_ONLY_MODE = 1000
    INVALID_AUTH_TOKEN = 1001
    INVALID_LOGIN = 1002
    LISTENER_NOT_AUTHORIZED = 1003
    PARTNER_NOT_AUTHORIZED = 1010


def pad_null(s, l):
    return s + "\0" * (l - len(s))


class PandoraJson:
    """
    Mixin for basic Pandora code.
    """

    class KEY:
        DEVICEMODEL = 'android-generic'
        USERNAME = 'android'
        PASSWORD = 'AC7IBG09A3DTSYM4R41UJWL07VLN8JI7'
        RPC_URL = '://tuner.pandora.com/services/json/?'
        ENCRYPT_KEY = '6#26FRL$ZWD'
        DECRYPT_KEY = 'R=U!LH$O2B#'
        VERSION = '5'

    def pandora_encrypt(self, s):
        return "".join(
            [self.blowfish_encode.encrypt(pad_null(s[i:i+8], 8)).encode('hex')
             for i in xrange(0, len(s), 8)])

    def pandora_decrypt(self, s):
        return "".join(
            [self.blowfish_decode.decrypt(pad_null(s[i:i+16].decode('hex'), 8))
             for i in xrange(0, len(s), 16)]).rstrip('\x08')


class PandoraJsonProtocol(WebSocketServerProtocol, PandoraJson):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """


class PandoraJsonFactory(WebSocketServerFactory, PandoraJson):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """

    def __init__(self, *args, **kwargs):
        self.username = kwargs.pop('username')
        self.password = kwargs.pop('password')
        self.proxy_host = kwargs.pop('proxy_host', None)
        self.proxy_port = kwargs.pop('proxy_port', 8080)
        WebSocketServerFactory.__init__(self, *args, **kwargs)
        self.connect()

    @defer.inlineCallbacks
    def json_call(self, method, **kwargs):
        https = kwargs.pop('https', False)
        blowfish = kwargs.pop('blowfish', True)

        url_args = {'method': method}
        if self.partner_id:
            url_args['partner_id'] = self.partner_id
        if self.user_id:
            url_args['user_id'] = self.user_id
        if self.user_auth_token or self.partner_auth_token:
            url_args['auth_token'] = (self.user_auth_token or
                                      self.partner_auth_token)

        protocol = 'https' if https else 'http'
        url = protocol + self.rpc_url + urllib.urlencode(url_args)

        if self.time_offset:
            kwargs['syncTime'] = int(time.time() + self.time_offset)
        if self.user_auth_token:
            kwargs['userAuthToken'] = self.user_auth_token
        elif self.partner_auth_token:
            kwargs['partnerAuthToken'] = self.partner_auth_token

        data = json.dumps(kwargs)

        if blowfish:
            data = self.pandora_encrypt(data)

        if self.proxy_host:
            endpoint = TCP4ClientEndpoint(reactor, self.proxy_host,
                                          self.proxy_port)
            agent = ProxyAgent(endpoint, WebClientContextFactory())
        else:
            agent = Agent(reactor, WebClientContextFactory())

        headers = Headers({'User-Agent': ['pithos'],
                           'Content-type': ['text/plain']})
        body = FileBodyProducer(StringIO(data))

        try:
            response = yield agent.request('POST', url, headers, body)
            body = yield readBody(response)
            tree = json.loads(body)
            if tree['stat'] == 'fail':
                code = tree['code']
                msg = tree['message']
                log.msg('fault code: {} message: {}'.format(code, msg))

                if code == API_ERROR.INVALID_AUTH_TOKEN:
                    raise PandoraAuthTokenInvalid(msg)
                elif code == API_ERROR.COUNTRY_NOT_SUPPORTED:
                    raise PandoraError(
                        "Pandora not available", code,
                        "Pandora is not available outside the US.")
                elif code == API_ERROR.API_VERSION_NOT_SUPPORTED:
                    raise PandoraAPIVersionError(msg)
                elif code == API_ERROR.INSUFFICIENT_CONNECTIVITY:
                    raise PandoraError(
                        "Out of sync", code, "Correct your system's clock.")
                elif code == API_ERROR.READ_ONLY_MODE:
                    raise PandoraError(
                        "Pandora maintenance", code,
                        "Pandora is in read-only mode as it is performing "
                        "maintenance. Try again later.")
                elif code == API_ERROR.INVALID_LOGIN:
                    raise PandoraError(
                        "Login Error", code, "Invalid username or password.")
                elif code == API_ERROR.LISTENER_NOT_AUTHORIZED:
                    raise PandoraError(
                        "Pandora One Error", code,
                        "A Pandora One account is required to access this "
                        "feature.")
                elif code == API_ERROR.PARTNER_NOT_AUTHORIZED:
                    raise PandoraError(
                        "Login Error", code, "Invalid Pandora partner keys.")
                else:
                    raise PandoraError(msg, code)

            if 'result' in tree:
                defer.returnValue(tree['result'])

        except Exception:
            failure.Failure().printTraceback()
            reactor.stop()

    @defer.inlineCallbacks
    def connect(self):
        self.partner_id = self.user_id = self.partner_auth_token = None
        self.user_auth_token = self.time_offset = None
        self.rpc_url = self.KEY.RPC_URL
        self.blowfish_encode = Blowfish(self.KEY.ENCRYPT_KEY)
        self.blowfish_decode = Blowfish(self.KEY.DECRYPT_KEY)

        partner = yield self.json_call('auth.partnerLogin',
                                       https=True,
                                       blowfish=False,
                                       deviceModel=self.KEY.DEVICEMODEL,
                                       username=self.KEY.USERNAME,
                                       password=self.KEY.PASSWORD,
                                       version=self.KEY.VERSION)

        self.partner_id = partner['partnerId']
        self.partner_auth_token = partner['partnerAuthToken']
        pandora_time = int(self.pandora_decrypt(partner['syncTime'])[4:14])
        self.time_offset = pandora_time - time.time()
        log.msg("The time offset is {}".format(self.time_offset))

        user = yield self.json_call('auth.userLogin',
                                    https=True,
                                    username=self.username,
                                    password=self.password,
                                    loginType='user')

        self.user_id = user['userId']
        self.user_auth_token = user['userAuthToken']
        defer.returnValue(True)


def main():
    log.startLogging(sys.stderr)

    if len(sys.argv) == 4:
        proxy = {"proxy_host": sys.argv[3]}
    elif len(sys.argv) == 5:
        proxy = {"proxy_host": sys.argv[3], "proxy_port": sys.argv[4]}
    else:
        proxy = {}

    factory = PandoraJsonFactory("ws://localhost:9000", debug=False,
                                 username=sys.argv[1], password=sys.argv[2],
                                 **proxy)
    factory.protocol = PandoraJsonProtocol
    listenWS(factory)

    reactor.run()

if __name__ == '__main__':
    main()
