import time

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import log


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


class PandoraMixin:
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

    AUDIO_FORMATS = {
        'highQuality': 'High',
        'mediumQuality': 'Medium',
        'lowQuality': 'Low',
    }

    RATE_BAN = 'ban'
    RATE_LOVE = 'love'
    RATE_NONE = None

    PLAYLIST_VALIDITY_TIME = 60*60*3

    def pandora_encrypt(self, s):
        return "".join(
            [self.blowfish_encode.encrypt(pad_null(s[i:i+8], 8)).encode('hex')
             for i in xrange(0, len(s), 8)])

    def pandora_decrypt(self, s):
        return "".join(
            [self.blowfish_decode.decrypt(pad_null(s[i:i+16].decode('hex'), 8))
             for i in xrange(0, len(s), 16)]).rstrip('\x08')

    audio_quality = AUDIO_FORMATS['highQuality']


class Station(object, PandoraMixin):
    def __init__(self, factory, **kwargs):
        self.factory = factory

        self.id = kwargs['stationId']
        self.id_token = kwargs['stationToken']
        self.is_creator = not kwargs['isShared']
        self.is_quick_mix = kwargs['isQuickMix']
        self.name = kwargs['stationName']
        self.use_quick_mix = False

        if self.is_quick_mix:
            self.factory.quick_mix_station_ids = kwargs.get(
                'quickMixStationIds', [])

    def __repr__(self):
        return u'<Station: {}>'.format(unicode(self))

    def __unicode__(self):
        return self.name

    @inlineCallbacks
    def transform_if_shared(self):
        if not self.is_creator:
            log.msg("pandora: transforming station")
            yield self.factory.json_call('station.transformSharedStation',
                                         stationToken=self.id_token)
            self.is_creator = True

    @inlineCallbacks
    def get_playlist(self):
        log.msg("pandora: Get Playlist")
        playlist = yield self.factory.json_call('station.getPlaylist',
                                                https=True,
                                                stationToken=self.id_token)
        songs = []
        for i in playlist['items']:
            if 'songName' in i:  # check for ads
                songs.append(Song(self.factory, **i))

        returnValue(songs)

    @property
    def info_url(self):
        return 'http://www.pandora.com/stations/'+self.id_token

    @inlineCallbacks
    def rename(self, new_name):
        if new_name != self.name:
            yield self.transform_if_shared()
            log.msg("pandora: Renaming station")
            yield self.factory.json_call('station.renameStation',
                                         stationName=new_name,
                                         stationToken=self.id_token)
            self.name = new_name

    @inlineCallbacks
    def delete(self):
        log.msg("pandora: Deleting Station")
        yield self.factory.json_call('station.deleteStation',
                                     stationToken=self.id_token)


class Song(object, PandoraMixin):
    def __init__(self, factory, **kwargs):
        self.factory = factory

        self.album = kwargs['albumName']
        self.artist = kwargs['artistName']
        self.audio_url_map = kwargs['audioUrlMap']
        self.track_token = kwargs['trackToken']
        # Banned songs won't play, so we don't care about them.
        self.rating = (self.RATE_LOVE if kwargs['songRating'] == 1
                       else self.RATE_NONE)
        self.station_id = kwargs['stationId']
        self.title = kwargs['songName']
        self.song_detail_url = kwargs['songDetailUrl']
        self.art_radio = kwargs['albumArtUrl']

        self.tired = False
        self.message = ''
        self.start_time = None
        self.finished = False
        self.playlist_time = time.time()
        self.feedback_id = None

    def __repr__(self):
        return u'<Song: {}>'.format(unicode(self))

    def __unicode__(self):
        return u'{} by {} from {}'.format(self.title, self.artist, self.album)

    @property
    def audio_url(self):
        quality = self.factory.audio_quality
        try:
            q = self.audio_url_map[quality]
            log.msg("Using audio quality {}: {} {}".format(quality,
                                                           q['bitrate'],
                                                           q['encoding']))
            return q['audioUrl']

        except KeyError:
            log.err("Unable to use audio format {}. Using {}".format(
                quality, self.audio_url_map.keys()[0]))
            return self.audio_url_map.values()[0]['audioUrl']

    @property
    def station(self):
        return self.factory.get_station_by_id(self.station_id)

    @inlineCallbacks
    def rate(self, rating):
        if self.rating != rating:
            self.station.transform_if_shared()
            if rating == self.RATE_NONE:
                if not self.feedback_id:
                    # We need a feedback_id, get one by re-rating the song. We
                    # could also get one by calling station.get_station, but
                    # that requires transferring a lot of data (all feedback,
                    # seeds, etc for the station).
                    opposite = (self.RATE_BAN if self.rating == self.RATE_LOVE
                                else self.RATE_LOVE)
                    self.feedback_id = yield self.factory.add_feedback(
                        self.track_token, opposite)
                yield self.factory.delete_feedback(self.station.id_token,
                                                   self.feedback_id)
            else:
                self.feedback_id = yield self.factory.add_feedback(
                    self.track_token, rating)
            self.rating = rating

    @inlineCallbacks
    def set_tired(self):
        if not self.tired:
            yield self.factory.json_call('user.sleepSong',
                                         track_token=self.track_token)
            self.tired = True

    @inlineCallbacks
    def bookmark(self):
        yield self.factory.json_call('bookmark.addSongBookmark',
                                     track_token=self.track_token)

    @inlineCallbacks
    def bookmark_artist(self):
        yield self.factory.json_call('bookmark.addArtistBookmark',
                                     track_token=self.track_token)

    @property
    def rating_str(self):
        return self.rating

    def is_still_valid(self):
        return (time.time() - self.playlist_time) < self.PLAYLIST_VALIDITY_TIME


class SearchResult(object):
    def __init__(self, result_type, **kwargs):
        self.result_type = result_type
        self.score = kwargs['score']
        self.musicId = kwargs['musicToken']

        if result_type == 'song':
            self.title = kwargs['songName']
            self.artist = kwargs['artistName']
        elif result_type == 'artist':
            self.name = kwargs['artistName']


__all__ = ['Station', 'Song', 'SearchResult', 'PandoraMixin', 'API_ERROR',
           'PandoraError', 'PandoraAuthTokenInvalid', 'PandoraNetError',
           'PandoraAPIVersionError', 'PandoraTimeout']
