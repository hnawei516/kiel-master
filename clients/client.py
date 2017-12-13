import logging
import time
import six
from tornado import gen, iostream, ioloop

from kiel.exc import BrokerConnectionError, UnhandledResponseError
from kiel.cluster import Cluster


log = logging.getLogger(__name__)


class Client(object):
    """
    Base class for all client classes.

    Handles basic cluster management and request sending.
    """
    def __init__(self, brokers):
        super(Client, self).__init__()

        self.cluster = Cluster(brokers)

        self.heal_cluster = False
        self.closing = False

    @gen.coroutine
    def connect(self):
        """
        Starts the underlying cluster, connecting and gathering metadata.
        """
        self.cluster.abort()
        yield self.cluster.start()

    @gen.coroutine
    def close(self):
        """
        Marks a client as closing and winds down connections.

        Calls the ``wind_down()`` coroutine that subclasses must implement.
        """
        self.closing = True

        yield self.wind_down()
        self.cluster.stop()

    @gen.coroutine
    def wind_down(self):
        """
        Cleanup method left to subclasses to define.

        Called by ``close()``, should clean up any subclass-specific resources.
        """
        raise NotImplementedError

    @gen.coroutine
    def send(self, request_by_broker):
        """
        Sends a dict of requests keyed on broker ID and handles responses.

        Returns a dictionary of the results of
        ``handle_<response.api>_response`` method calls, keyed to the
        corresponding broker ID.

        Raises ``UnhandledResponseError`` if the client subclass does not have
        a ``handle_<response.api>_response`` method available to handle an
        incoming response object.

        If an error occurs in a response, the ``heal_cluster`` flag is set
        and the ``heal()`` method on the cluster is called after processing
        each response.

        Responses are handled in the order they come in, but this method does
        not yield a value until all responses are handled.
        """
        iterator = gen.WaitIterator(**{
            str(broker_id): self.cluster[broker_id].send(request)
            for broker_id, request in six.iteritems(request_by_broker)
        })

        results = {}

        while not iterator.done():
            self.heal_check = time.time()
            try:
                response = yield iterator.next()
            except BrokerConnectionError as e:
                log.info("Connection to %s:%s lost", e.host, e.port)
                self.heal_cluster = True
                break
            except iostream.StreamClosedError:
                log.info("Connection to broker lost.")
                self.heal_cluster = True
                break
            except Exception:
                log.exception("Error sending request.")
                self.heal_cluster = True
                continue
            
            self.heal_check = time.time()
            handler = getattr(self, "handle_%s_response" % response.api, None)
            if handler is None:
                raise UnhandledResponseError(response.api)

            result = yield gen.maybe_future(handler(response))
            results[int(iterator.current_index)] = result

        if self.heal_cluster:
            self.heal_cluster = False
            yield self.cluster.heal()

        raise gen.Return(results)
