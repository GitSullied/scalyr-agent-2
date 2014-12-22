# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# author: Steven Czerwinski <czerwin@scalyr.com>

__author__ = 'czerwin@scalyr.com'

import httplib
import os
import platform
import re
import socket
import sys
import time

# noinspection PyBroadException
try:
    import ssl
    __has_ssl__ = True
except Exception:
    __has_ssl__ = False
    ssl = None

import scalyr_agent.json_lib as json_lib
import scalyr_agent.scalyr_logging as scalyr_logging
import scalyr_agent.util as scalyr_util

from cStringIO import StringIO

log = scalyr_logging.getLogger(__name__)


class ScalyrClientSession(object):
    """Encapsulates the connection between the agent and the Scalyr servers.

    It is a session in that we generally only have one connection open to the Scalyr servers at any given time.
    The session aspect is important because we must ensure that the timestamps we include in the AddEventRequests
    are monotonically increasing within a session.
    """
    def __init__(self, server, api_key, agent_version, quiet=False, request_deadline=60.0, ca_file=None):
        """Initializes the connection.

        This does not actually try to connect to the server.
        @param server: The URL for the server to send requests to, such as https://agent.scalyr.com
        @param api_key: The write logs key to use to authenticate all requests from this agent to scalyr.
            It both authenticates the requests and identifies which account it belongs to.
        @param agent_version: The agent version number, which is included in request headers sent to the server.
        @param quiet: If True, will not log non-error information.
        @param request_deadline: The maximum time to wait for all requests in seconds.
        @param ca_file: The path to the file containing the certificates for the trusted certificate authority roots.
            This is used for the SSL connections to verify the connection is to Scalyr.

        @type server: str
        @type api_key: str
        @type agent_version: str
        @type quiet: bool
        @type request_deadline: float
        @type ca_file: str
        """
        if not quiet:
            log.info('Using "%s" as address for scalyr servers' % server)
        # Verify the server address looks right.
        parsed_server = re.match('^(http://|https://|)([^:]*)(:\d+|)$', server.lower())

        if parsed_server is None:
            raise Exception('Could not parse server address "%s"' % server)

        # The full URL address
        self.__full_address = server
        # The host for the server.
        self.__host = parsed_server.group(2)
        # Whether or not the connection uses SSL.  For production use, this should always be true.  We only
        # use non-SSL when testing against development versions of the Scalyr server.
        self.__use_ssl = parsed_server.group(1) == 'https://'

        # Determine the port, defaulting to the right one based on protocol if not given.
        if parsed_server.group(3) != '':
            self.__port = int(parsed_server.group(3)[1:])
        elif self.__use_ssl:
            self.__port = 443
        else:
            self.__port = 80

        # The HTTPConnection object that has been opened to the servers, if one has been opened.
        self.__connection = None
        self.__api_key = api_key
        self.__session_id = scalyr_util.create_unique_id()
        # The time of the last success.
        self.__last_success = None

        # The last time the connection was closed, if any.
        self.__last_connection_close = None

        # We create a few headers ahead of time so that we don't have to recreate them each time we need them.
        self.__standard_headers = {
            'Connection': 'Keep-Alive',
            'Accept': 'application/json',
            'User-Agent': ScalyrClientSession.__get_user_agent(agent_version)
        }

        # The number of seconds to wait for a blocking operation on the connection before considering it to have
        # timed out.
        self.__request_deadline = request_deadline

        # The total number of RPC requests sent.
        self.total_requests_sent = 0
        # The total number of RPC requests that failed.
        self.total_requests_failed = 0
        # The total number of bytes sent over the network.
        self.total_request_bytes_sent = 0
        # The total number of bytes received.
        self.total_response_bytes_received = 0
        # The total number of secs spent waiting for a responses (so average latency can be calculated by dividing
        # this number by self.total_requests_sent).  This includes connection establishment time.
        self.total_request_latency_secs = 0
        # The total number of HTTP connections successfully created.
        self.total_connections_created = 0
        # The path the file containing the certs for the root certificate authority to use for verifying the SSL
        # connection to Scalyr.  If this is None, then server certificate verification is disabled, and we are
        # susceptible to man-in-the-middle attacks.
        self.__ca_file = ca_file

    def ping(self):
        """Ping the Scalyr server by sending a test message to add zero events.

        If the returned message is 'success', then it has been verified that the agent can connect to the
        configured Scalyr server and that the api key is correct.

        @return:  The status message returned by the server.
        @rtype:
        """
        return self.send(self.add_events_request())[0]

    def send(self, add_events_request):
        """Sends an AddEventsRequest to Scalyr.

        The AddEventsRequest should have been retrieved using the 'add_events_request' method on this object.

        @param add_events_request: The request containing any log lines/events to copy to the server.

        @type add_events_request: AddEventsRequest

        @return: A tuple containing the status message in the response (such as 'success'), the number of bytes
            sent, and the full response.
        @rtype: (str, int, str)
        """
        current_time = time.time()

        # Refuse to try to send the message if the connection has been recently closed and we have not waited
        # long enough to try to re-open it.  We do this to avoid excessive connection opens and SYN floods.
        if self.__last_connection_close is not None and current_time - self.__last_connection_close < 30:
            return 'client/connectionClosed', 0, ''

        self.total_requests_sent += 1
        was_success = False
        bytes_received = 0

        if self.__use_ssl:
            if not __has_ssl__:
                log.warn('No ssl library available so cannot verify server certificate when communicating with Scalyr. '
                         'This means traffic is encrypted but can be intercepted through a man-in-the-middle attack. '
                         'To solve this, install the Python ssl library. '
                         'For more details, see https://www.scalyr.com/help/scalyr-agent#ssl',
                         limit_once_per_x_secs=86400, limit_key='nosslwarning', error_code='client/nossl')
            elif self.__ca_file is None:
                log.warn('Server certificate validation has been disabled while communicating with Scalyr. '
                         'This means traffic is encrypted but can be intercepted through a man-in-the-middle attach. '
                         'Please update your configuration file to re-enable server certificate validation.',
                         limit_once_per_x_secs=86400, limit_key='nocertwarning', error_code='client/sslverifyoff')

        # TODO:  Break this part out into a generic invokeApi method once we need to support
        # multiple Scalyr service API calls.
        response = ''
        try:
            try:
                if self.__connection is None:
                    if self.__use_ssl:
                        # If we do not have the SSL library, then we cannot do server certificate validation anyway.
                        if __has_ssl__:
                            ca_file = self.__ca_file
                        else:
                            ca_file = None
                        self.__connection = HTTPSConnectionWithTimeoutAndVerification(self.__host, self.__port,
                                                                                      self.__request_deadline,
                                                                                      ca_file, __has_ssl__)

                    else:
                        self.__connection = HTTPConnectionWithTimeout(self.__host, self.__port, self.__request_deadline)
                    self.__connection.connect()
                    self.total_connections_created += 1
            except (socket.error, socket.herror, socket.gaierror), error:
                if hasattr(error, 'errno'):
                    errno = error.errno
                else:
                    errno = None
                if __has_ssl__ and isinstance(error, ssl.SSLError):
                    log.error('Failed to connect to "%s" due to some SSL error.  Possibly the configured certificate '
                              'for the root Certificate Authority could not be parsed, or we attempted to connect to '
                              'a server whose certificate could not be trusted (if so, maybe Scalyr\'s SSL cert has '
                              'changed and you should update your agent to get the new certificate).  The returned '
                              'errno was %d and the full exception was \'%s\'.  Closing connection, will re-attempt',
                              self.__full_address, errno, str(error), error_code='client/connectionFailed')
                elif errno == 61:  # Connection refused
                    log.error('Failed to connect to "%s" because connection was refused.  Server may be unavailable.',
                              self.__full_address, error_code='client/connectionFailed')
                elif errno == 8:  # Unknown name
                    log.error('Failed to connect to "%s" because could not resolve address.  Server host may be bad.',
                              self.__full_address, error_code='client/connectionFailed')
                elif errno is not None:
                    log.error('Failed to connect to "%s" due to errno=%d.  Exception was %s.  Closing connection, '
                              'will re-attempt', self.__full_address, errno, str(error),
                              error_code='client/connectionFailed')
                else:
                    log.error('Failed to connect to "%s" due to exception.  Exception was %s.  Closing connection, '
                              'will re-attempt', self.__full_address, str(error),
                              error_code='client/connectionFailed')
                return 'client/connectionFailed', 0, ''

            # Update the time that request it is being sent, according the client's clock.
            add_events_request.set_client_time(current_time)

            body_str = add_events_request.get_payload()

            self.total_request_bytes_sent += len(body_str)

            # noinspection PyBroadException
            try:
                log.log(scalyr_logging.DEBUG_LEVEL_5, 'Sending POST /addEvents with body \"%s\"', body_str)
                self.__connection.request('POST', '/addEvents', body=body_str,
                                          headers=self.__standard_headers)

                response = self.__connection.getresponse().read()
                bytes_received = len(response)
            except Exception, error:
                # TODO: Do not just catch Exception.  Do narrower scope.
                if hasattr(error, 'errno'):
                    log.error('Failed to connect to "%s" due to errno=%d.  Exception was %s.  Closing connection, '
                              'will re-attempt', self.__full_address, error.errno, str(error),
                              error_code='client/requestFailed')
                else:
                    log.exception('Failed to send request due to exception.  Closing connection, will re-attempt',
                                  error_code='requestFailed')
                return 'requestFailed', len(body_str), response

            log.log(scalyr_logging.DEBUG_LEVEL_5, 'Response was received with body \"%s\"', response)

            # If we got back an empty result, that often means the connection has been closed or reset.
            if len(response) == 0:
                log.error('Received empty response, server may have reset connection.  Will re-attempt',
                          error_code='emptyResponse')
                return 'emptyResponse', len(body_str), response

            # Try to parse the response
            # noinspection PyBroadException
            try:
                response_as_json = json_lib.parse(response)
            except Exception:
                # TODO: Do not just catch Exception.  Do narrower scope.  Also, log error here.
                log.exception('Failed to parse response of \'%s\' due to exception.  Closing connection, will '
                              're-attempt', scalyr_util.remove_newlines_and_truncate(response, 1000),
                              error_code='parseResponseFailed')
                return 'parseResponseFailed', len(body_str), response

            self.__last_success = current_time

            if 'status' in response_as_json:
                status = response_as_json['status']
                if status == 'success':
                    was_success = True
                elif status == 'error/client/badParam':
                    log.error('Request to \'%s\' failed due to a bad parameter value.  This may be caused by an '
                              'invalid write logs api key in the configuration', self.__full_address,
                              error_code='error/client/badParam')
                else:
                    log.error('Request to \'%s\' failed due to an error.  Returned error code was \'%s\'',
                              self.__full_address, status, error_code='error/client/badParam')
                return status, len(body_str), response
            else:
                log.error('No status message provided in response.  Unknown error.  Response was \'%s\'',
                          scalyr_util.remove_newlines_and_truncate(response, 1000), error_code='unknownError')
                return 'unknownError', len(body_str), response

        finally:
            self.total_request_latency_secs += (time.time() - current_time)
            if not was_success:
                self.total_requests_failed += 1
                self.close(current_time=current_time)
            self.total_response_bytes_received += bytes_received

    def close(self, current_time=None):
        """Closes the underlying connection to the Scalyr server.

        @param current_time: If not None, the time to use for the current time.  Used for testing purposes.
        @type current_time: float or None
        """
        if self.__connection is not None:
            if current_time is None:
                current_time = time.time()
            self.__connection.close()
            self.__connection = None
            self.__last_connection_close = current_time

    def add_events_request(self, session_info=None, max_size=1*1024*1024*1024):
        """Creates and returns a new AddEventRequest that can be later sent by this session.

        The caller is expected to add events to this request and then submit it for transmission using
        the 'send' method.

        @param session_info: The session info for this session, which is basically any attributes that should
            be added to all events uploaded by this agent, such as server attributes from the config file.
        @param max_size: The maximum number of bytes to send in this request.

        @type session_info: dict
        @type max_size: int

        @return:  The request that can be populated.
        @rtype: AddEventsRequest
        """
        body = {
            'token': self.__api_key,
            'session': self.__session_id,
            'threads': [],
        }

        if session_info is not None:
            body['sessionInfo'] = session_info

        return AddEventsRequest(body, max_size=max_size)

    @staticmethod
    def __get_user_agent(agent_version):
        """Determine the user agent to report in the request headers.

        We construct an agent that gives Scalyr some information about the platform the customer is running on,
        the Python version, and a few other tidbits.  This is used to make decisions about support issues.

        @param agent_version: The agent version number.
        @type agent_version: str

        @return: The user agent string.
        @rtype: str
        """
        # We will construct our agent string to look something like:
        # Linux-redhat-7.0;python-2.7.2;agent-2.0.1;ssllib

        python_version = sys.version_info
        if len(python_version) >= 5:
            python_version_str = 'python-%s.%s.%s' % (python_version[0], python_version[1], python_version[2])
        else:
            python_version_str = 'python-unknown'

        # Try for a linux distribution first.  This doesn't seem to work for Amazon AMIs, but for most
        # distributions it hopefully will provide something readable.
        platform_value = None
        # noinspection PyBroadException
        try:
            distribution = platform.dist()
            if len(distribution[0]) > 0:
                platform_value = 'Linux-%s-%s' % (distribution[0], distribution[1])
        except Exception:
            platform_value = None

        # Try Mac
        if platform_value is None:
            # noinspection PyBroadException
            try:
                mac_ver = platform.mac_ver()[0]
                if len(mac_ver) > 0:
                    platform_value = 'MacOS-%s' % mac_ver
            except Exception:
                platform_value = None

        # Fall back for all others.  This should print out something reasonable for
        # Windows.
        if platform_value is None:
            platform_value = platform.platform(terse=1)

        # Include a string to indicate if python has a true ssl library available to record
        # whether or not the client is doing server certificate verification.
        if __has_ssl__:
            ssl_str = 'ssllib'
        else:
            ssl_str = 'nossllib'

        return '%s;%s;agent-%s;%s;' % (platform_value, python_version_str, agent_version, ssl_str)


class AddEventsRequest(object):
    """Used to construct an AddEventsRequest to eventually send.

    This abstraction has three key features.  First, it uses a generally more efficient scheme to build
    up the string to eventually use as the body for an add_events request.  Secondly, it does not require all events
    at construction time.  Instead, you can incrementally add more events before the request is actually sent.  This
    leads to better memory utilization when combined with an abstraction that is incrementally reading events from disk.
    It will also prevent you from exceeding the maximum request size.  Third, you may undo the effect of adding events
    to the request before it is sent.  This is useful to rollback the request state to a previous state if some
    problem occurs.
    """
    def __init__(self, base_body, max_size=1*1024*1024):
        """Initializes the instance.

        @param base_body: A JsonObject or dict containing the information to send as the body of the add_events
            request, with the exception of the events field. The events and client_timestamp fields must not be
            included because they will be added later. Note, base_body must have some fields set, such as 'ts' which is
            required by the server.
        @param max_size: The maximum number of bytes this request can consume when it is serialized to JSON.
        """
        assert len(base_body) > 0, "The base_body object must have some fields defined."
        assert not 'events' in base_body, "The base_body object cannot already have 'events' set."
        assert not 'client_time' in base_body, "The base_body object cannot already have 'client_time' set."

        # As an optimization, we use a StringIO object to serialize the request.  We also
        # do a little bit of the JSON object assembly by hand.  Specifically, we serialize the request
        # to JSON without the 'events' field, but then delete the last '}' so that we can manually
        # add in the 'events: [ ... ]' ourselves.  This way we can watch the size of the buffer as
        # we build up events.
        string_buffer = StringIO()
        json_lib.serialize(base_body, output=string_buffer, use_fast_encoding=True)

        # Now go back and find the last '}' and delete it so that we can open up the JSON again.
        location = string_buffer.tell()
        while location > 0:
            location -= 1
            string_buffer.seek(location)
            if string_buffer.read(1) == '}':
                break

        # Now look for the first non-white character.  We need to add in a comma after it.
        last_char = None
        while location > 0:
            location -= 1
            string_buffer.seek(location)
            last_char = string_buffer.read(1)
            if not last_char.isspace():
                break

        # If the character happened to a comma, back up over that since we want to write our own comma.
        if location > 0 and last_char == ',':
            location -= 1

        if location < 0:
            raise Exception('Could not locate trailing "}" and non-whitespace in base JSON for add events request')

        # Now chop off everything after the character at the location.
        location += 1
        string_buffer.seek(location)
        string_buffer.truncate()

        # Append the start of our events field.
        string_buffer.write(', events: [')

        # The string that must be append after all of the events to terminate the JSON.  We will
        # later replace TIMESTAMP with the real timestamp and replace THREADS with the serialized threads array.
        self.__post_fix = '], threads: THREADS, client_time: TIMESTAMP }'

        # The time that will be sent as the 'client_time' parameter for the addEvents request.
        # This may be later updated using the set_client_time method in the case where the same AddEventsRequest
        # is being reused to send the events again.
        self.__client_time = time.time()

        # Holds a list of dicts, one for each thread added so far.
        self.__threads = []

        self.__buffer = string_buffer
        self.__max_size = max_size
        self.__current_size = self.__buffer.tell() + len(self.__get_post_fix(self.__client_time))

        self.__events_added = 0

        # If we have finished serializing the body, it is stored here until the close() method is invoked.
        self.__body = None

    def add_thread(self, thread_id, thread_name):
        """Registers the specified thread for this AddEvents request.

        Any thread id mentioned in any event in this request should first be registered here.

        @param thread_id: An id for the thread.  This can then be used as the value for a ``thread`` field
            in the ``event`` object passed to ``add_event``.  Should be unique for this session.
        @param thread_name: A human-readable name for the thread

        @type thread_id: str
        @type thread_name: str

        @return: True if there was the allowed bytes to send were not exceeded by adding this thread to the
            request.
        @rtype: bool
        """
        # Have to account for the extra space this will use when serialized.  For now, we do a little heavy weight
        # thing and just size how big the post fix size is compared to the old size.
        original_size = len(self.__get_post_fix(self.__client_time))

        self.__threads.append({"id": thread_id, "name": thread_name})

        added_size = len(self.__get_post_fix(self.__client_time)) - original_size

        if self.__current_size + added_size > self.__max_size:
            self.__threads.pop()
            return False

        self.__current_size += added_size
        return True

    def add_event(self, event, timestamp=None):
        """Adds the serialized JSON for event if it does not cause the maximum request size to be exceeded.

        It will automatically add in a 'ts' field to event containing a new timestamp based on the current time
        but ensuring it is greater than any previous timestamp that has been used.

        It is illegal to invoke this method if 'get_payload' has already been invoked.

        @param event: The event object, usually a dict or a JsonObject.
        @param timestamp: The timestamp to use for the event. This should only be used for testing.

        @return: True if the event's serialized JSON was added to the request, or False if that would have resulted
            in the maximum request size being exceeded so it did not.
        """
        start_pos = self.__buffer.tell()
        # If we already added an event before us, then make sure we add in a comma to separate us from the last event.
        if self.__events_added > 0:
            self.__buffer.write(',')

        if timestamp is None:
            timestamp = self.__get_timestamp()

        event['ts'] = str(timestamp)
        json_lib.serialize(event, output=self.__buffer, use_fast_encoding=True)
        size = self.__buffer.tell() - start_pos

        # Check if we exceeded the size, if so chop off what we just added.
        if self.__current_size + size > self.__max_size:
            self.__buffer.truncate(start_pos)
            return False

        self.__current_size += size
        self.__events_added += 1
        return True

    def set_client_time(self, current_time):
        """Update the 'client_time' field in the request.

        The 'client_time' field should be set to the current time as known by the client when this request is
        sent.  Since a AddEventsRequest can be re-used multiple times to try to resend some events, it is important
        to update the 'client_time' field before each send.

        The 'client_time' field is used by the server to warn when the client clock skew is too great since that
        can lead to log upload problems.

        @param current_time: The current time to include in the request.
        @type current_time: float
        """
        if self.__body is not None:
            # We have already cached the serialized JSON, so we need to update it to remain consistent.
            old_post_fix = self.__get_post_fix(self.__client_time)
            new_post_fix = self.__get_post_fix(current_time)

            # Create a buffer for the copying.  We write in the entire JSON and then just back up the length of
            # the old postfix to then add in the new one.
            rebuild_buffer = StringIO()
            rebuild_buffer.write(self.__body)
            self.__body = None
            rebuild_buffer.seek(-1 * len(old_post_fix), os.SEEK_END)
            rebuild_buffer.truncate()
            rebuild_buffer.write(new_post_fix)
            self.__body = rebuild_buffer.getvalue()
            rebuild_buffer.close()

        self.__client_time = current_time

    def get_payload(self):
        """Returns the serialized JSON to use as the body for the add_request.

        After this is invoked, no new events can be added via the 'add_event' method.  However,
        you may call the 'set_client_time' method to update when this request is being sent, according to
        the client clock.
        """
        if self.__body is None:
            self.__buffer.write(self.__get_post_fix(self.__client_time))
            self.__body = self.__buffer.getvalue()
            self.__buffer.close()
            self.__buffer = None
        return self.__body

    def close(self):
        """Must be invoked after this request is no longer needed.  You may not add events or invoke get_payload
        after this call.
        """
        self.__body = None

    def __get_post_fix(self, client_time):
        """Returns the string that should be appended after the events JSON array to complete the
        JSON for the body.

        @param client_time: The time in seconds past epoch to include in this request for the client time.

        @return: The post fix string, including the client time parameter.
        """
        tmp = self.__post_fix.replace('TIMESTAMP', str(int(client_time)))
        return tmp.replace('THREADS', json_lib.serialize(self.__threads))

    def __get_timestamp(self):
        """
        @return: The next timestamp to use for events.  This is guaranteed to be monotonically increasing.
        @rtype: long
        """
        global __last_time_stamp__

        base_timestamp = long(time.time() * 1000000000L)
        if __last_time_stamp__ is not None and base_timestamp <= __last_time_stamp__:
            base_timestamp = __last_time_stamp__ + 1L
        __last_time_stamp__ = base_timestamp
        return base_timestamp

    @property
    def total_events(self):
        """Returns the total number of events that will be sent in this batch."""
        return self.__events_added

    def position(self):
        """Returns a position such that if it is passed to 'set_position', all events added since this method was
        invoked are removed."""

        return AddEventsRequest.Position(self.__current_size, self.__events_added, self.__buffer.tell(),
                                         len(self.__threads))

    def set_position(self, position):
        """Reverts this object to only contain the events contained by the object when position was invoked to
        get the passed in position.

        @param position: The position token representing the previous state.
        """
        self.__current_size = position.current_size
        self.__events_added = position.events_added
        self.__buffer.truncate(position.buffer_size)
        assert position.thread_count <= len(self.__threads)
        self.__threads = self.__threads[0:position.thread_count]

    class Position(object):
        """Represents a position in the added events.
        """
        def __init__(self, current_size, events_added, buffer_size, thread_count):
            self.current_size = current_size
            self.events_added = events_added
            self.buffer_size = buffer_size
            self.thread_count = thread_count

# The last timestamp used for any event uploaded to the server.  We need to guarantee that this is monotonically
# increasing so we track it in a global var.
__last_time_stamp__ = None


class HTTPConnectionWithTimeout(httplib.HTTPConnection):
    """An HTTPConnection replacement with added support for setting a timeout on all blocking operations.

    Older versions of Python (2.4, 2.5) do not allow for setting a timeout directly on httplib.HTTPConnection
    objects.  This is meant to solve that problem generally.
    """
    def __init__(self, host, port, timeout):
        self.__timeout = timeout
        httplib.HTTPConnection.__init__(self, host, port)

    def connect(self):
        # This method is essentially copied from 2.7's httplib.HTTPConnection.connect.
        # If socket.create_connection then we use it (as it does in newer Pythons), otherwise, rely on our
        # own way of doing it.
        if hasattr(socket, 'create_connection'):
            self.sock = socket.create_connection((self.host, self.port), self.__timeout)
        else:
            self.sock = create_connection_helper(self.host, self.port, timeout=self.__timeout)
        if self._tunnel_host:
            self._tunnel()


class HTTPSConnectionWithTimeoutAndVerification(httplib.HTTPSConnection):
    """An HTTPSConnection replacement that adds support for setting a timeout as well as performing server
    certificate validation.

    Older versions of Python (2.4, 2.5) do not allow for setting a timeout directly on httplib.HTTPConnection
    objects, nor do they perform validation of the server certificate.  However, if the user installs the ssl
    Python library, then it is possible to perform server certificate validation even on Python 2.4, 2.5.  This
    class implements the necessary support.
    """
    def __init__(self, host, port, timeout, ca_file, has_ssl):
        """
        Creates an instance.

        Params:
            host: The server host to connect to.
            port: The port to connect to.
            timeout: The timeout, in seconds, to use for all blocking operations on the underlying socket.
            ca_file:  If not None, then this is a file containing the certificate authority's root cert to use
                for validating the certificate sent by the server.  This must be None if has_ssl is False.
                If None is passed in, then no validation of the server certificate will be done whatsoever, so
                you will be susceptible to man-in-the-middle attacks.  However, at least your traffic will be
                encrypted.
            has_ssl:  True if the ssl Python library is available.
        """
        if not has_ssl and ca_file is not None:
            raise Exception('If has_ssl is false, you are not allowed to specify a ca_file because it has no affect.')
        self.__timeout = timeout
        self.__ca_file = ca_file
        self.__has_ssl = has_ssl
        httplib.HTTPSConnection.__init__(self, host, port)

    def connect(self):
        # If the ssl library is not available, then we just have to fall back on old HTTPSConnection.connect
        # method.  There are too many dependencies to implement it directly here.
        if not self.__has_ssl:
            # Unfortunately, the only way to set timeout is to temporarily set the global default timeout
            # for what it should be for this connection, and then reset it once the connection is established.
            # Messy, but not much we can do.
            old_timeout = None
            try:
                old_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(self.__timeout)
                httplib.HTTPSConnection.connect(self)
                return
            finally:
                socket.setdefaulttimeout(old_timeout)

        # Create the underlying socket.  Prefer Python's newer socket.create_connection method if it is available.
        if hasattr(socket, 'create_connection'):
            self.sock = socket.create_connection((self.host, self.port), self.__timeout)
        else:
            self.sock = create_connection_helper(self.host, self.port, timeout=self.__timeout)

        if self._tunnel_host:
            self._tunnel()

        # Now ask the ssl library to wrap the socket and verify the server certificate if we have a ca_file.
        if self.__ca_file is not None:
            self.sock = ssl.wrap_socket(self.sock, ca_certs=self.__ca_file, cert_reqs=ssl.CERT_REQUIRED)
        else:
            self.sock = ssl.wrap_socket(self.sock, cert_reqs=ssl.CERT_NONE)


def create_connection_helper(host, port, timeout=None, source_address=None):
    """Creates and returns a socket connecting to host:port with the specified timeout.

    @param host: The host to connect to.
    @param port: The port to connect to.
    @param timeout: The timeout in seconds to use for all blocking operations on the socket.
    @param source_address: The source address, or None.

    @return: The connected socket
    """
    # This method was copied from Python 2.7's socket.create_connection.
    err = None
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(sa)
            return sock

        except socket.error, _:
            err = _
            if sock is not None:
                sock.close()

    if err is not None:
        raise err
    else:
        raise socket.error("getaddrinfo returns an empty list")
