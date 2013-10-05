SocialSound
===========

SocialSound apsires to be a Pandora® player that is useable as a home or
office jukebox on a LAN. It connects to a specified Pandora® account, and
allows users to connect to the server and cooperatively choose the station that
will play, voting on station additions, removals, changes, and song upvotes
and downvotes.

SocialSound is written in Python using Twisted and Autobahn for Websockets. The
frontend will be written in AngularJS. Database backend for SocialSound is not
decided yet but will probably be file-based such as SQLite or LevelDB. Whatever
it is it needs to work nicely with Twisted's concurrency model.

SocialSound includes a lot of code and ideas taken from
[pithos](https://github.com/pithos/pithos) which is an excellent desktop player
for Pithos on Linux.

Pithos is licensed GPLv3 but SocialSound is licensed AGPLv3 because it is
probably not wise to run this as a public SaaS product.
