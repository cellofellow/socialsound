import json
import os
import sys
import time
import urllib
from cStringIO import StringIO
from itertools import izip as zip

from configobj import ConfigObj
import xdg.BaseDirectory

from twisted.internet import reactor, task
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.ssl import ClientContextFactory
from twisted.python import failure, log
from twisted.web.client import Agent, readBody, FileBodyProducer
from twisted.web.client import ProxyAgent
from twisted.web.http_headers import Headers
from twisted.internet.endpoints import TCP4ClientEndpoint

from autobahn.websocket import (WebSocketServerFactory,
                                WebSocketServerProtocol,
                                listenWS)
from mpd import MPDFactory, MPD_HOST, MPD_PORT

from blowfish import Blowfish
from pandora import (PandoraMixin, Station, SearchResult, API_ERROR,
                     PandoraError, PandoraAuthTokenInvalid,
                     PandoraAPIVersionError)


class SocialSoundError(Exception):
    pass


class WebClientContextFactory(ClientContextFactory):
    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)


class PandoraProtocol(WebSocketServerProtocol, PandoraMixin):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """

    def onOpen(self):
        self.factory.register(self)
        self.sendMessage("SocialSound WebSocket")

    def connectionLost(self, reason):
        self.factory.unregister(self)
        WebSocketServerProtocol.connectionLost(self, reason)

    def onMessage(self, msg, binary):
        split = msg.split(' ', 1)
        if len(split) == 1:
            command, arg = msg, ''
        else:
            command, arg = split

        method = getattr(self, 'cmd_' + command, self.bad_command)
        method(arg)

    def bad_command(self, arg):
        self.sendMessage("Bad command.")


    def cmd_stations(self, _):
        stations = self.factory.stations
        resp = [{'name': s.name, 'id': s.id} for s in stations]
        message = json.dumps(resp, indent=4)
        self.sendMessage(message)

    @inlineCallbacks
    def cmd_play_station(self, station_id):
        try:
            yield self.factory.play_station(station_id)
        except (PandoraError, SocialSoundError) as e:
            self.sendMessage("Error: " + e.message)
        except Exception:
            self.sendMessage("There was a server error.")
            failure.Failure().printTraceback()

    @inlineCallbacks
    def cmd_stop(self, _):
        self.factory.station = None
        yield self.factory.player.p.stop()
        yield self.factory.player.p.clear()


class PandoraFactory(WebSocketServerFactory, PandoraMixin):
    """
    Connect to the Pandora JSON API and provide a websocket interface to it.
    """

    def __init__(self, *args, **kwargs):
        self.username = kwargs.pop('username')
        self.password = kwargs.pop('password')
        self.proxy_host = kwargs.pop('proxy_host', None)
        self.proxy_port = kwargs.pop('proxy_port', 8080)
        self.clients = set()
        WebSocketServerFactory.__init__(self, *args, **kwargs)
        self.initialize()

    @inlineCallbacks
    def initialize(self):
        try:
            yield self.connect()
            yield self.login()
            yield self.get_stations()
        except:
            failure.Failure().printTraceback()
            reactor.stop()

    def register(self, client):
        self.clients.add(client)

    def unregister(self, client):
        self.clients.remove(client)

    @inlineCallbacks
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
            returnValue(tree['result'])

    @inlineCallbacks
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

    @inlineCallbacks
    def login(self):
        if not all((hasattr(self, 'partner_id'),
                    hasattr(self, 'partner_auth_token'))):
            yield self.connect()

        user = yield self.json_call(
            'auth.userLogin', https=True, username=self.username,
            password=self.password, loginType='user')

        self.user_id = user['userId']
        self.user_auth_token = user['userAuthToken']

    @inlineCallbacks
    def get_stations(self):
        response = yield self.json_call('user.getStationList')
        stations = response['stations']
        self.stations = [Station(self, **s) for s in stations]

        if self.quick_mix_station_ids:
            for s in self.stations:
                if s.id in self.quick_mix_station_ids:
                    s.use_quick_mix = True

        returnValue(self.stations)

    @inlineCallbacks
    def save_quick_mix(self):
        station_ids = [s.id for s in self.stations if s.use_quick_mix]

        yield self.json_call('user.setQuickMix',
                             quickMixStationIds=station_ids)

    @inlineCallbacks
    def search(self, query):
        response = yield self.json_call('music.search', searchText=query)
        results = [SearchResult('artist', **a) for a in response['artists']]
        results += [SearchResult('song', **s) for s in response['songs']]
        results.sort(key=lambda i: i.score, reverse=True)

        returnValue(results)

    @inlineCallbacks
    def add_station_by_music_id(self, music_id):
        response = yield self.json_call('station.createStation',
                                        musicToken=music_id)
        station = Station(self, **response)
        self.stations.append(station)

        returnValue(station)

    def get_station_by_id(self, id):
        for s in self.stations:
            if s.id == id:
                return s

    def get_song_by_mpd_id(self, song_id):
        for song in playlist:
            if get(song, 'mpd_id', None) == song_id:
                return song

    @inlineCallbacks
    def add_feedback(self, track_token, rating):
        log.msg("pandora: addFeedback")
        rating = True if rating == self.RATE_LOVE else False
        feedback = yield self.json_call('station.addFeedback',
                                        trackToken=track_token,
                                        isPositive=rating)

        returnValue(feedback['feedbackId'])

    @inlineCallbacks
    def delete_feedback(self, station_token, feedback_id):
        yield self.json_call('station.deleteFeedback', feedbackId=feedback_id,
                             stationToken=station_token)

    @inlineCallbacks
    def get_playlist(self):
        station = getattr(self, 'station', None)
        if not station:
            raise SocialSoundError("No station set.")
        playlist = yield station.get_playlist()
        self.playlist = playlist
        returnValue(playlist)

    @inlineCallbacks
    def play_station(self, station_id):
        log.msg(station_id)
        player = self.player.p
        station = self.get_station_by_id(station_id)
        if not station:
            raise SocialSoundError(
                "Station {} not found".format(station_id))
        self.station = station
        player.stop()
        player.clear()
        while self.station == station:
            yield self.get_playlist()
            player.command_list_ok_begin()
            for song in self.playlist:
                player.addid(song.audio_url)
            result_list = yield player.command_list_end()
            for mpd_id, song in zip(result_list, self.playlist):
                song.mpd_id = mpd_id
            yield player.playid(self.playlist[0].mpd_id)

            songs = iter(self.playlist)
            song = songs.next()
            while self.station == station:
                try:
                    status = yield player.status()
                    log.msg(status)
                    log.msg(song)

                    current_song_id = status.get('songid', None)
                    log.msg("Current Song MPD id: {}".format(current_song_id))
                    if current_song_id is None:
                        break
                    if current_song_id != song.mpd_id:
                        log.msg("Moving to next song...")
                        song = songs.next()

                    status['current_song'] = unicode(song)
                    message = json.dumps(status, indent=4)
                    log.msg(self.clients)
                    for c in self.clients:
                        c.sendMessage(message)
                    yield task.deferLater(reactor, 1, lambda: None)
                except StopIteration:
                    log.msg("Playlist exhausted. Fetching another...")
                    break
                except Exception:
                    failure.Failure().printTraceback()


class PlayerFactory(MPDFactory):
    def connectionMade(self, protocol):
        self.p = protocol


def main():
    log.startLogging(sys.stderr)

    conf_file = os.path.join(
        xdg.BaseDirectory.save_config_path('socialsound'),
        'config.ini')
    config = ConfigObj(conf_file)

    username = config['Pandora']['username']
    password = config['Pandora']['password']

    proxy = config.get('Proxy', {})
    proxy_host = proxy.get('host', None)
    proxy_port = proxy.get('port', None)
    proxy = {}
    if proxy_host: proxy['proxy_host'] = proxy_host
    if proxy_port: proxy['proxy_port'] = proxy_port

    # Start websocket listener
    factory = PandoraFactory("ws://localhost:9000", debug=False,
                             username=username, password=password, **proxy)
    factory.protocol = PandoraProtocol
    listenWS(factory)

    # Start the MPD client factory.
    factory.player = PlayerFactory()
    reactor.connectTCP(MPD_HOST, MPD_PORT, factory.player)

    reactor.run()

if __name__ == '__main__':
    main()
