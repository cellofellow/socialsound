import json
import sys
from urllib import urlencode

from twisted.internet import defer, task, reactor
from twisted.python import failure, log
from twisted.web.client import Agent, readBody

from autobahn.websocket import (WebSocketServerFactory,
                                WebSocketServerProtocol,
                                listenWS)


class BitcoinConverterProtocol(WebSocketServerProtocol):
    """
    Receive prices in various currencies and return the equivilant in Bitcoin.
    """

    convert_url = 'http://blockchain.info/tobtc'

    help_text = (
        "Enter a currency followed by an amount to convert it to bitcoin.\n"
        "Currency codes:\n"
        "USD, CNY, JPY, SGD, HKD, CAD, AUD, NZD, "
        "GBP, DKK, SEK, BRL, CHF, EUR, RUB, SLL"
    )

    def onOpen(self):
        self.factory.register(self)
        self.sendMessage("Welcome to the Bitcoin Converter Websocket")

    def onMessage(self, msg, binary):
        if msg == 'help':
            self.sendMessage(self.help_text)
        else:
            self.convertCurrency(msg)

    @defer.inlineCallbacks
    def convertCurrency(self, line):
        currency, value = line.split(' ')
        getopts = '?' + urlencode({'currency': currency, 'value': value})
        agent = Agent(reactor)
        try:
            response = yield agent.request('GET', self.convert_url + getopts)
            body = yield readBody(response)
            if response.code == 200:
                self.sendMessage('BTC ' + body)
            else:
                self.sendMessage('Error: ' + body)
        except Exception:
            failure.Failure().printTraceback()

    def connectionLost(self, reason):
        self.factory.unregister(self)
        WebSocketServerProtocol.connectionLost(self, reason)


class BroadcastServerFactory(WebSocketServerFactory):
    """
    Server factory for BitcoinConverterProtocol to add a broadcast ticker with
    the lastest price.
    """

    ticker_url = 'http://blockchain.info/ticker'

    def __init__(self, *args, **kwargs):
        WebSocketServerFactory.__init__(self, *args, **kwargs)
        self.clients = set()
        self.ticker()

    def register(self, client):
        self.clients.add(client)

    def unregister(self, client):
        self.clients.remove(client)

    @defer.inlineCallbacks
    def ticker(self):
        agent = Agent(reactor)
        while True:
            try:
                response = yield agent.request('GET', self.ticker_url)
                body = yield readBody(response)
                data = json.loads(body)
                log.msg("Sending broadcast.")
                message = "Last Price: ${} USD".format(data['USD']['last'])
                for c in self.clients:
                    c.sendMessage(message)
                yield task.deferLater(reactor, 60, lambda: None)
            except Exception:
                failure.Failure().printTraceback()


def main():
    log.startLogging(sys.stderr)
    factory = BroadcastServerFactory("ws://localhost:9000", debug=False)
    factory.protocol = BitcoinConverterProtocol
    listenWS(factory)

    reactor.run()

if __name__ == '__main__':
    main()
