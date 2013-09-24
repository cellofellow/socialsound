import json
import sys
import time
import urllib
from cStringIO import StringIO

from twisted.internet import defer, reactor
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
from pandora import (PandoraMixin, Station, SearchResult, API_ERROR,
                     PandoraError, PandoraAuthTokenInvalid,
                     PandoraAPIVersionError)


class WebClientContextFactory(ClientContextFactory):
    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)


class PandoraProtocol(WebSocketServerProtocol, PandoraMixin):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """

    def bad_command(self, arg):
        self.sendMessage("Bad command.")

    @defer.inlineCallbacks
    def cmd_login(self, arg):
        if all((hasattr(self.factory, 'user_id'),
                hasattr(self.factory, 'user_auth_token'))):
            self.sendMessage("Already logged in.")
        else:
            username, password = arg.split(' ', 1)
            try:
                yield self.factory.login(username, password)
                self.sendMessage("Success")
            except PandoraError as e:
                self.sendMessage("Error: " + e.message)
            except Exception as e:
                self.sendMessage("There was a server error.")
                failure.Failure().printTraceback()

    @defer.inlineCallbacks
    def cmd_stations(self, arg):
        try:
            stations = yield self.factory.get_stations()
            station_data = [{'name': s.name, 'id': s.id} for s in stations]
            self.sendMessage(json.dumps(station_data, indent=4))
        except PandoraError as e:
            self.sendMessage("Error: " + e.message)
        except Exception as e:
            self.sendMessage("There was a server error.")
            failure.Failure().printTraceback()

    @defer.inlineCallbacks
    def cmd_get_playlist(self, arg):
        try:
            station = None
            if not arg and hasattr(self.factory, 'current_station'):
                station = self.factory.current_station
            else:
                station = self.factory.get_station_by_id(arg)

            if station:
                playlist = yield station.get_playlist()
                self.factory.current_station = station
                self.factory.current_playlist = playlist
                self.cmd_playlist(None)
            else:
                self.sendMessage("Station {} not found.".format(arg))

        except PandoraError as e:
            self.sendMessage("Error: " + e.message)
        except Exception as e:
            self.sendMessage("There was a server error.")
            failure.Failure().printTraceback()

    cmd_set_station = cmd_get_playlist

    def cmd_playlist(self, arg):
        try:
            playlist = getattr(self.factory, 'current_playlist', None)
            if playlist:
                song_names = [unicode(s) for s in playlist]
                self.sendMessage(json.dumps(song_names, indent=4))
            else:
                self.sendMessage("No playlist set.")

        except PandoraError as e:
            self.sendMessage("Error: " + e.message)
        except Exception as e:
            self.sendMessage("There was a server error.")
            failure.Failure().printTraceback()

    def onMessage(self, msg, binary):
        split = msg.split(' ', 1)
        if len(split) == 1:
            command, arg = msg, ''
        else:
            command, arg = split

        method = getattr(self, 'cmd_' + command, self.bad_command)
        method(arg)


class PandoraFactory(WebSocketServerFactory, PandoraMixin):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """

    def __init__(self, *args, **kwargs):
        self.proxy_host = kwargs.pop('proxy_host', None)
        self.proxy_port = kwargs.pop('proxy_port', 8080)
        WebSocketServerFactory.__init__(self, *args, **kwargs)

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

    @defer.inlineCallbacks
    def login(self, username, password):
        if not all((hasattr(self, 'partner_id'),
                    hasattr(self, 'partner_auth_token'))):
            yield self.connect()

        user = yield self.json_call(
            'auth.userLogin', https=True, username=username, password=password,
            loginType='user')

        self.user_id = user['userId']
        self.user_auth_token = user['userAuthToken']

    @defer.inlineCallbacks
    def get_stations(self):
        response = yield self.json_call('user.getStationList')
        stations = response['stations']
        self.stations = [Station(self, **s) for s in stations]

        if self.quick_mix_station_ids:
            for s in self.stations:
                if s.id in self.quick_mix_station_ids:
                    s.use_quick_mix = True

        defer.returnValue(self.stations)

    @defer.inlineCallbacks
    def save_quick_mix(self):
        station_ids = []
        for s in self.stations:
            if s.use_quick_mix:
                station_ids.append(s.id)

        yield self.json_call('user.setQuickMix',
                             quickMixStationIds=station_ids)

    @defer.inlineCallbacks
    def search(self, query):
        response = yield self.json_call('music.search', searchText=query)
        results = [SearchResult('artist', **a) for a in response['artists']]
        results += [SearchResult('song', **s) for s in response['songs']]
        results.sort(key=lambda i: i.score, reverse=True)

        defer.returnValue(results)

    @defer.inlineCallbacks
    def add_station_by_music_id(self, music_id):
        response = yield self.json_call('station.createStation',
                                        musicToken=music_id)
        station = Station(self, **response)
        self.stations.append(station)

        defer.returnValue(station)

    def get_station_by_id(self, id):
        for s in self.stations:
            if s.id == id:
                return s

    @defer.inlineCallbacks
    def add_feedback(self, track_token, rating):
        log.msg("pandora: addFeedback")
        rating = True if rating == self.RATE_LOVE else False
        feedback = yield self.json_call('station.addFeedback',
                                        trackToken=track_token,
                                        isPositive=rating)

        defer.returnValue(feedback['feedbackId'])

    @defer.inlineCallbacks
    def delete_feedback(self, station_token, feedback_id):
        yield self.json_call('station.deleteFeedback', feedbackId=feedback_id,
                             stationToken=station_token)


def main():
    log.startLogging(sys.stderr)

    if len(sys.argv) == 2:
        proxy = {"proxy_host": sys.argv[1]}
    elif len(sys.argv) == 3:
        proxy = {"proxy_host": sys.argv[1], "proxy_port": sys.argv[2]}
    else:
        proxy = {}

    factory = PandoraFactory("ws://localhost:9000", debug=False, **proxy)
    factory.protocol = PandoraProtocol
    listenWS(factory)

    reactor.run()

if __name__ == '__main__':
    main()
